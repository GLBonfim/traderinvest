"""M5 pipeline validation: timeframe/causality, purge, costs, sessions, rules, limits, gate,
protocol-before-data and the temporal holdout discipline (synthetic data only)."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.fx.agents.trained import (
    BAR_HOURS,
    FEATURE_NAMES,
    LinearModel,
    feature_frame,
    load_models,
    model_signals,
    signals_from_features,
)
from app.fx.research import m5_validation as m5
from app.fx.research.m5_validation import (
    BASE_COSTS,
    PROTOCOL,
    STRESS_COSTS,
    M5Protocol,
    gate,
    holdout_eligible,
    mid_of,
    pattern_frame,
    pipeline_signals,
    simulate,
    training_rows,
)

N = len(FEATURE_NAMES)


def m5_frame(
    start: str = "2024-03-04",
    days: int = 6,
    seed: int = 1,
    drift: float = 0.0,
    spread: float = 0.00006,
) -> pd.DataFrame:
    """Weekday M5 bid/ask bars (weekends are real holes)."""
    idx = pd.date_range(start, periods=days * 288, freq="5min", tz="UTC")
    idx = idx[idx.dayofweek < 5]
    rng = np.random.default_rng(seed)
    mid = 1.10 + np.cumsum(rng.normal(drift, 0.00012, len(idx)))
    o = np.concatenate([[mid[0]], mid[:-1]])
    h = np.maximum(o, mid) + rng.uniform(0, 0.0001, len(idx))
    lo = np.minimum(o, mid) - rng.uniform(0, 0.0001, len(idx))
    out = {}
    for side, off in (("bid", -spread / 2), ("ask", spread / 2)):
        out |= {
            f"{side}_open": o + off,
            f"{side}_high": h + off,
            f"{side}_low": lo + off,
            f"{side}_close": mid + off,
        }
    return pd.DataFrame(out, index=idx)


def model(algorithm: str = "ridge", intercept: float = -1.0, timeframe: str = "M5") -> LinearModel:
    """A constant forecast: always short (intercept below -threshold) where features exist."""
    return LinearModel(
        f"t-{algorithm}",
        algorithm,
        timeframe,
        (0.0,) * N,
        (1.0,) * N,
        (0.0,) * N,
        intercept,
        0.35 if algorithm == "ridge" else 0.55,
        "2020-01-01T00:00:00+00:00",
        max_hold=12,
    )


# --- timeframe and causality --------------------------------------------------------------


def test_m5_is_a_model_timeframe_but_legacy_h1_h4_map_is_unchanged() -> None:
    assert BAR_HOURS == {"H1": 1, "H4": 4}  # tournament recipes and pilot resampling intact
    assert model().timeframe == "M5"
    with pytest.raises(ValueError):
        model(timeframe="M1")


def test_m5_session_features_use_the_bar_close() -> None:
    df = m5_frame("2024-03-05", days=2)
    f = feature_frame(mid_of(df), "M5")
    # 07:55 London (winter: UTC) closes at 08:00, when London becomes liquid
    assert bool(f.loc[pd.Timestamp("2024-03-05 07:55", tz="UTC"), "tradable"])
    assert not bool(f.loc[pd.Timestamp("2024-03-05 07:50", tz="UTC"), "tradable"])
    angle = f.loc[pd.Timestamp("2024-03-05 11:55", tz="UTC"), "hour_cos"]
    assert angle == pytest.approx(np.cos(2 * np.pi * 12 / 24))  # 12:00 close


def test_features_and_patterns_ignore_future_bars() -> None:
    df = m5_frame(days=4)
    cut = len(df) - 50
    altered = df.copy()
    altered.iloc[cut:] = altered.iloc[cut:] * 1.03
    f1 = feature_frame(mid_of(df), "M5").iloc[:cut]
    f2 = feature_frame(mid_of(altered), "M5").iloc[:cut]
    pd.testing.assert_frame_equal(f1, f2)
    p1 = pattern_frame(mid_of(df)).iloc[:cut]
    p2 = pattern_frame(mid_of(altered)).iloc[:cut]
    pd.testing.assert_frame_equal(p1, p2)
    truncated = pattern_frame(mid_of(df.iloc[:cut]))
    pd.testing.assert_frame_equal(truncated, p1)


def test_signals_from_features_equals_model_signals() -> None:
    mid = mid_of(m5_frame(days=3))
    m = model()
    pd.testing.assert_frame_equal(
        model_signals(mid, m), signals_from_features(feature_frame(mid, "M5"), m)
    )


# --- purge --------------------------------------------------------------------------------


def test_training_labels_are_purged_with_embargo_and_never_span_gaps() -> None:
    df = m5_frame("2024-03-04", days=12)  # includes a weekend hole
    frames = {"EURUSD": df}
    feats = {"EURUSD": feature_frame(mid_of(df), "M5")}
    before = pd.Timestamp("2024-03-13", tz="UTC")
    p = replace(PROTOCOL, min_training_rows=10)
    x, y = training_rows(frames, feats, pd.Timestamp("2024-03-04", tz="UTC"), before, p)
    assert len(x) == len(y) > 100
    # recompute which rows were kept: label end + embargo strictly before the cutoff
    idx = pd.Series(df.index, index=df.index)
    end = idx.shift(-p.horizon_bars) + m5.BAR
    kept_max_end = end[(end < before - pd.Timedelta(hours=24))].max()
    assert kept_max_end < before - pd.Timedelta(hours=24)
    weekend = (end - idx) != (p.horizon_bars + 1) * m5.BAR
    assert weekend.any()  # the hole exists, and such rows are excluded below
    h = p.horizon_bars
    y_all = (mid_of(df)["close"].shift(-h) - mid_of(df)["open"].shift(-1)) / feats["EURUSD"]["atr"]
    assert not np.isin(y, y_all[weekend.to_numpy()].dropna().clip(-20, 20).to_numpy()).all()


# --- costs --------------------------------------------------------------------------------


def _one_trade(
    df: pd.DataFrame, j: int, stop: float = 0.002, hold: int = 12, costs: Any = BASE_COSTS
) -> pd.DataFrame:
    sig = pd.DataFrame(
        {"dir": 0, "stop_dist": stop, "target_dist": stop * 1.5, "score": 0.0, "max_hold": hold},
        index=df.index,
    )
    sig.iloc[j, sig.columns.get_loc("dir")] = -1
    sig.iloc[j, sig.columns.get_loc("score")] = 0.9
    return simulate({"EURUSD": df}, {"EURUSD": sig}, df.index[0], df.index[-1], costs)


def test_capped_size_and_costs_at_the_amended_geometry() -> None:
    df = m5_frame(days=2)
    t = _one_trade(df, 300)  # 20-pip stop: 11 USD / 0.002 = 5 500 units, capped at 5 000
    assert len(t) == 1 and t["units"].iat[0] == 5_000
    expected = 2 * 2.0 / (5_000 * 0.002)  # min 2 USD per side beats 0.2 bps of 5 500 USD
    assert t["r_cost"].iat[0] == pytest.approx(expected)  # 0.40 R
    s = _one_trade(df, 300, costs=STRESS_COSTS)
    assert s["r_cost"].iat[0] == pytest.approx(expected * 1.5)
    assert s["entry"].iat[0] == pytest.approx(t["entry"].iat[0] - 0.00002)  # 0.2 pip worse


def test_old_geometry_is_vetoed_for_fees() -> None:
    df = m5_frame(days=2)
    assert len(_one_trade(df, 300, stop=0.0006)) == 0  # 1.33 R of fees: never simulated
    assert m5.fee_ratio("EURUSD", 5_000, 1.1, 0.0006) == pytest.approx(4 / 3)


def test_fee_floor_and_target_per_pair() -> None:
    floors = {s: m5.fee_floor(s) for s in ("EURUSD", "EURGBP", "EURJPY")}
    assert floors["EURUSD"] == pytest.approx(4 / 0.45 / 5_000)  # 17.8 pips
    assert floors["EURGBP"] == pytest.approx(4 * 0.77 / 0.45 / 5_000)
    assert floors["EURJPY"] == pytest.approx(4 * 150 / 0.45 / 5_000)  # 26.7 JPY pips
    for sym, floor in floors.items():
        assert m5.fee_ratio(sym, 5_000, 1.0, floor) == pytest.approx(0.45)


def test_fee_veto_applies_after_rounding_units_down() -> None:
    df = m5_frame(days=2)
    # 58 pips: 11 USD / 0.0058 = 1 896 units -> 1 000 (never forced up) -> 5.8 USD of risk:
    # 4 USD of fees = 0.69 R > 0.45, so the trade is vetoed even though the stop is wide
    assert len(_one_trade(df, 300, stop=0.0058)) == 0
    assert len(_one_trade(df, 300, stop=0.0058, costs=STRESS_COSTS)) == 0  # same trade set
    kept = _one_trade(df, 300, stop=0.005)  # 2 200 -> 2 000 units, 10 USD risk: 0.40 R
    assert len(kept) == 1 and kept["units"].iat[0] == 2_000
    assert kept["units"].iat[0] * 0.005 <= 11.0  # never above the 0.1 % risk budget


def test_amended_protocol_holds_96_bars_with_coherent_purge() -> None:
    assert PROTOCOL.horizon_bars == 96 and PROTOCOL.target_rr == 2.0
    proto = PROTOCOL.to_dict()
    assert proto["amendment"]["superseded"]["horizon_bars"] == 12
    assert "no performance" in proto["amendment"]["basis"]
    df = m5_frame(days=3)
    t = _one_trade(df, 100, stop=0.01, hold=96)  # unreachable stop/target
    assert t["reason"].iat[0] == "time" and t["bars"].iat[0] == 96


def test_sell_enters_on_next_bid_open_and_stop_wins_same_bar() -> None:
    df = m5_frame(days=2)
    j = 300
    df.iloc[j + 1, df.columns.get_loc("ask_high")] = df["bid_open"].iat[j + 1] + 0.01
    df.iloc[j + 1, df.columns.get_loc("ask_low")] = df["bid_open"].iat[j + 1] - 0.01
    t = _one_trade(df, j)
    assert t["entry"].iat[0] == pytest.approx(df["bid_open"].iat[j + 1])
    assert t["reason"].iat[0] == "stop" and t["r_gross"].iat[0] == pytest.approx(-1.0)


def test_time_stop_and_weekend_hole_close_the_trade() -> None:
    df = m5_frame(days=2)
    t = _one_trade(df, 300, stop=0.01, hold=12)  # unreachable stop/target
    assert t["reason"].iat[0] == "time" and t["bars"].iat[0] == 12
    fri = m5_frame("2024-03-08", days=4)  # Friday then a weekend hole
    last_fri = int(np.flatnonzero(fri.index.dayofweek == 4)[-1])
    t2 = _one_trade(fri, last_fri - 5, stop=0.01, hold=12)
    assert t2["reason"].iat[0] == "gap" and t2["exit_ts"].iat[0].dayofweek == 4


# --- lab limits ---------------------------------------------------------------------------


def test_lab_limits_five_per_day_three_open_one_per_pair() -> None:
    frames = {s: m5_frame(days=2, seed=i) for i, s in enumerate(("EURUSD", "EURGBP", "EURJPY"))}
    sigs = {}
    for s, df in frames.items():
        sig = pd.DataFrame(
            {"dir": -1, "stop_dist": 0.01, "target_dist": 0.015, "score": 0.9, "max_hold": 12},
            index=df.index,
        )
        sigs[s] = sig
    t = simulate(frames, sigs, frames["EURUSD"].index[0], frames["EURUSD"].index[-1], BASE_COSTS)
    t["day"] = m5._trading_day_ordinal(pd.DatetimeIndex(t["entry_ts"]))
    assert t.groupby("day").size().max() <= 5
    for _, g in t.groupby("pair"):  # never two at once on one pair
        assert (g["entry_ts"].iloc[1:].to_numpy() > g["exit_ts"].iloc[:-1].to_numpy()).all()
    points = sorted(
        [(x, 1) for x in t["entry_ts"]] + [(x + m5.BAR, -1) for x in t["exit_ts"]],
        key=lambda p: (p[0], p[1]),
    )
    level = peak = 0
    for _, delta in points:
        level += delta
        peak = max(peak, level)
    assert peak <= 3


# --- specialist rules ---------------------------------------------------------------------


def _ctx(n: int, **cols: Any) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    idx = pd.date_range("2024-03-05 10:00", periods=n, freq="5min", tz="UTC")
    df = pd.DataFrame({"bid_close": 1.1, "ask_close": 1.10006}, index=idx)
    f = pd.DataFrame({name: 0.0 for name in FEATURE_NAMES}, index=idx)
    f["atr"], f["er"], f["volatility_rank"], f["sma_gap"], f["tradable"] = (
        0.0003,
        0.3,
        0.5,
        -1.0,
        True,
    )
    for k, v in cols.items():
        f[k] = v
    patterns = pd.DataFrame(
        0.0,
        index=idx,
        columns=[
            f"{fam}_{o}"
            for fam in ("reversal", "continuation", "momentum")
            for o in ("bull", "bear")
        ],
    )
    return df, f, patterns


@pytest.mark.parametrize(
    "role,family",
    [("reversao", "reversal"), ("continuacao", "continuation"), ("impulso", "momentum")],
)
def test_each_specialist_needs_its_real_family_sell_and_agreement(role: str, family: str) -> None:
    df, f, pats = _ctx(5)
    pats[f"{family}_bear"] = 0.8
    sig = pipeline_signals(df, f, pats, role, model(), "EURUSD")
    floor = m5.fee_floor("EURUSD")  # 2 ATR (6 pips) is below the fee floor
    assert (sig["dir"] == -1).all() and np.allclose(sig["stop_dist"], floor)
    assert np.allclose(sig["target_dist"], 2 * floor) and (sig["max_hold"] == 96).all()
    other = next(x for x in ("reversal", "continuation", "momentum") if x != family)
    pats2 = pats.copy() * 0
    pats2[f"{other}_bear"] = 0.8  # another family's pattern is not this specialist's
    assert (pipeline_signals(df, f, pats2, role, model(), "EURUSD")["dir"] == 0).all()
    disagree = model(intercept=1.0)  # the auxiliary model forecasts up
    assert (pipeline_signals(df, f, pats, role, disagree, "EURUSD")["dir"] == 0).all()
    weak = pats * 0 + 0.3  # below the frozen minimum strength
    assert (pipeline_signals(df, f, weak, role, model(), "EURUSD")["dir"] == 0).all()


def test_bullish_is_vetoed_never_inverted_and_trend_rules() -> None:
    df, f, pats = _ctx(5, sma_gap=1.0)  # analyst trend up
    pats["continuation_bull"] = 0.9
    assert (pipeline_signals(df, f, pats, "continuacao", model(), "EURUSD")["dir"] == 0).all()
    pats["continuation_bear"] = 0.9  # bearish against an up trend: not a continuation
    assert (pipeline_signals(df, f, pats, "continuacao", model(), "EURUSD")["dir"] == 0).all()
    df, f, pats = _ctx(5, er=0.9)  # too efficient a move for a reversal
    pats["reversal_bear"] = 0.9
    assert (pipeline_signals(df, f, pats, "reversao", model(), "EURUSD")["dir"] == 0).all()
    df, f, pats = _ctx(5)
    pats["reversal_bull"], pats["reversal_bear"] = 0.9, 0.9  # a tie never becomes a SELL
    assert (pipeline_signals(df, f, pats, "reversao", model(), "EURUSD")["dir"] == 0).all()


def test_analyst_vetoes_untradable_wide_spread_and_extreme_volatility() -> None:
    for cols in ({"tradable": False}, {"volatility_rank": 0.99}):
        df, f, pats = _ctx(3, **cols)
        pats["momentum_bear"] = 0.9
        assert (pipeline_signals(df, f, pats, "impulso", model(), "EURUSD")["dir"] == 0).all()
    df, f, pats = _ctx(3)
    pats["momentum_bear"] = 0.9
    df["ask_close"] = df["bid_close"] + 0.0004  # 4 pips
    assert (pipeline_signals(df, f, pats, "impulso", model(), "EURUSD")["dir"] == 0).all()


# --- gate ---------------------------------------------------------------------------------


def _trades(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"r_net": values, "r_cost": 0.1, "bars": 5})


def test_gate_requires_every_frozen_condition() -> None:
    good = {"2024": _trades([0.5, -0.3] * 20), "2025": _trades([0.5, -0.3] * 20)}
    assert gate(good, _trades([0.2] * 80))["passed"]
    assert not gate(
        {"2024": _trades([0.5, -0.3] * 10), "2025": _trades([0.5, -0.3] * 10)}, _trades([0.2] * 40)
    )["passed"]  # 40 < 60 closed
    one_bad = {"2024": _trades([0.5, -0.3] * 30), "2025": _trades([-0.1] * 40)}
    assert not gate(one_bad, _trades([0.2] * 100))["checks"]["positive_periods"]
    assert not gate(good, _trades([-0.01] * 80))["checks"]["stress_mean_r"]
    thin = {"2024": _trades([0.105, -0.1] * 20), "2025": _trades([0.105, -0.1] * 20)}
    assert not gate(thin, _trades([0.2] * 80))["checks"]["profit_factor"]  # PF 1.05 < 1.1


def test_inactivity_and_holdout_eligibility() -> None:
    empty = pd.DataFrame(columns=["r_net", "r_cost", "bars"])
    result = gate({"2024": empty, "2025": empty}, empty)
    assert not result["passed"] and result["pooled"]["trades"] == 0
    assert not holdout_eligible(_trades([0.5] * 19))["eligible_demo"]  # < 20 trades
    assert not holdout_eligible(_trades([-0.1] * 30))["eligible_demo"]  # not positive
    assert holdout_eligible(_trades([0.2, -0.1] * 10))["eligible_demo"]


# --- runner discipline --------------------------------------------------------------------


def _tiny_protocol(**kw: Any) -> M5Protocol:
    base = replace(
        PROTOCOL,
        train_start="2024-03-04T00:00:00+00:00",
        folds=(
            ("a", "2024-03-18T00:00:00+00:00", "2024-03-25T00:00:00+00:00"),
            ("b", "2024-03-25T00:00:00+00:00", "2024-04-01T00:00:00+00:00"),
        ),
        holdout_start="2024-04-01T00:00:00+00:00",
        min_training_rows=200,
        forest_trees=4,
    )
    return replace(base, **kw)


def _loader(out: Path, calls: list[tuple[str, Any]]) -> Any:
    data = {
        s: m5_frame("2024-03-04", days=42, seed=i)
        for i, s in enumerate(("EURUSD", "EURGBP", "EURJPY"))
    }

    def load(root: Path, symbol: str, before: pd.Timestamp | None) -> pd.DataFrame:
        assert (out / "protocol.json").exists()  # frozen BEFORE any data is read
        if before is None:
            assert (out / "selection.json").exists()  # holdout only after the selection
        calls.append((symbol, before))
        df = data[symbol]
        return df if before is None else df[df.index < before]

    return load


def test_runner_freezes_protocol_first_and_no_trade_when_gate_fails(tmp_path: Path) -> None:
    calls: list[tuple[str, Any]] = []
    reg = m5.run(
        tmp_path / "data",
        tmp_path,
        _tiny_protocol(),
        _loader(tmp_path, calls),
        require_manifest=False,
        say=lambda _: None,
    )
    assert reg["active_model_ids"] == [] and reg["statistically_approved"] is False
    assert all(b is not None for _, b in calls)  # the holdout was never loaded
    cands = json.loads((tmp_path / "candidates.json").read_text(encoding="utf-8"))["candidates"]
    assert len(cands) == 6 and {c["role"] for c in cands} == set(m5.ROLES)
    proto = json.loads((tmp_path / "protocol.json").read_text(encoding="utf-8"))
    assert reg["protocol_sha256"] == m5.digest(proto)
    assert set(reg["no_trade_roles"]) == set(m5.ROLES)
    assert load_models(tmp_path / "selected.json") == []
    with pytest.raises(SystemExit, match="holdout"):
        m5.run(
            tmp_path / "data",
            tmp_path,
            _tiny_protocol(),
            _loader(tmp_path, []),
            require_manifest=False,
            say=lambda _: None,
        )


def test_runner_evaluates_holdout_once_after_selection(tmp_path: Path) -> None:
    calls: list[tuple[str, Any]] = []
    lax = _tiny_protocol(
        gate_min_closed=0,
        gate_min_positive_periods=0,
        gate_min_profit_factor=0.0,
        gate_min_mean_r=-1e9,
        gate_min_stress_mean_r=-1e9,
        holdout_min_trades=0,
        holdout_min_total_r=-1e9,
        min_pattern_strength=0.0,
        require_model_agreement=False,
    )
    reg = m5.run(
        tmp_path / "data",
        tmp_path,
        lax,
        _loader(tmp_path, calls),
        require_manifest=False,
        say=lambda _: None,
    )
    assert any(b is None for _, b in calls)  # the holdout was loaded, after selection
    hold = json.loads((tmp_path / "holdout.json").read_text(encoding="utf-8"))
    sel = json.loads((tmp_path / "selection.json").read_text(encoding="utf-8"))
    assert hold["selection_sha256"] == m5.digest(sel)
    models = load_models(tmp_path / "selected.json")
    assert {m.timeframe for m in models} == {"M5"} and len(models) == len(reg["active_model_ids"])
    assert all(m.training_end == lax.holdout_start for m in models)
    selected = json.loads((tmp_path / "selected.json").read_text(encoding="utf-8"))
    assert selected["pipelines"]  # every exported model carries its full pipeline
    for pipe in selected["pipelines"].values():
        assert pipe["required_full_pipeline"] is True and pipe["max_hold"] == 96
        assert pipe["target_rr"] == 2.0 and pipe["max_fee_risk"] == 0.45
        assert set(pipe["fee_floor"]) == {"EURUSD", "EURGBP", "EURJPY"}


def test_manifest_must_be_complete(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        m5.check_manifest(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps({"complete": False}), encoding="utf-8")
    with pytest.raises(ValueError, match="complete"):
        m5.check_manifest(tmp_path)


def test_loader_dedupes_overlapping_chunks_and_truncates(tmp_path: Path) -> None:
    df = m5_frame(days=2)
    d = tmp_path / "EURUSD"
    d.mkdir()
    for side in ("BID", "ASK"):
        cols = {k: df[f"{side.lower()}_{k}"] for k in ("open", "high", "low", "close")}
        part = pd.DataFrame(cols).reset_index(names="ts")
        part.iloc[:400].to_parquet(d / f"{side}_M5_20240305.parquet")
        part.iloc[300:].to_parquet(d / f"{side}_M5_20240306.parquet")  # 100 bars overlap
    out = m5.load_m5(tmp_path, "EURUSD")
    assert len(out) == len(df) and out.index.is_unique and out.index.is_monotonic_increasing
    cut = df.index[200]
    assert m5.load_m5(tmp_path, "EURUSD", cut).index.max() < cut


def test_entry_requires_the_next_bar_exactly_five_minutes_later() -> None:
    df = m5_frame(days=2)
    j = 300
    holed = df.drop(df.index[j + 1])  # a missing bar: the next one is 10 min later
    t = _one_trade(holed, j, stop=0.01)
    assert len(t) == 0
    assert len(_one_trade(df, j, stop=0.01)) == 1


def test_trades_never_use_data_after_the_fold_end() -> None:
    df = m5_frame(days=2)
    j = 300
    sig = pd.DataFrame(
        {"dir": 0, "stop_dist": 0.01, "target_dist": 0.015, "score": 0.0, "max_hold": 12},
        index=df.index,
    )
    sig.iloc[j, 0], sig.iloc[j, 3] = -1, 0.9
    hi = df.index[j + 5]  # the fold ends 4 bars after the entry
    t = simulate({"EURUSD": df}, {"EURUSD": sig}, df.index[0], hi, BASE_COSTS)
    assert t["reason"].iat[0] == "boundary" and t["exit_ts"].iat[0] < hi
    assert t["exit"].iat[0] == pytest.approx(df["ask_close"].iat[j + 4])
    late = simulate({"EURUSD": df}, {"EURUSD": sig}, df.index[0], df.index[j + 1], BASE_COSTS)
    assert len(late) == 0  # the entry bar itself would be outside the fold


def test_runner_writes_every_fold_model_and_trade_list(tmp_path: Path) -> None:
    m5.run(
        tmp_path / "data",
        tmp_path,
        _tiny_protocol(),
        _loader(tmp_path, []),
        require_manifest=False,
        say=lambda _: None,
    )
    folds = sorted((tmp_path / "models-folds").glob("*.json"))
    assert len(folds) == 12  # 6 candidates x 2 folds, rejected ones included
    cands = json.loads((tmp_path / "candidates.json").read_text(encoding="utf-8"))["candidates"]
    for c in cands:
        for fold in c["folds"]:
            path = tmp_path / "models-folds" / f"{fold['model_id']}.json"
            assert m5.digest(json.loads(path.read_text(encoding="utf-8"))) == fold["model_sha256"]
            for kind in ("base", "stress"):
                csv = tmp_path / "trades" / f"{c['candidate']}__{fold['fold']}__{kind}.csv"
                assert csv.is_file()
    text = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "POR CANDIDATO" in text and "teste conjunto" in text


def _write_chunks(root: Path, df: pd.DataFrame, symbol: str = "EURUSD") -> list[dict[str, Any]]:
    import hashlib

    d = root / symbol
    d.mkdir(parents=True, exist_ok=True)
    chunks = []
    for side in ("BID", "ASK"):
        cols = {k: df[f"{side.lower()}_{k}"] for k in ("open", "high", "low", "close")}
        f = d / f"{side}_M5_20240306.parquet"
        pd.DataFrame(cols).reset_index(names="ts").to_parquet(f)
        chunks.append(
            {
                "pair": symbol,
                "side": side,
                "file": f"data/fx/ibkr-m5/{symbol}/{f.name}".replace("/", "\\"),
                "rows": len(df),
                "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
            }
        )
    return chunks


def test_manifest_hashes_counts_and_unlisted_files(tmp_path: Path) -> None:
    df = m5_frame(days=1)
    chunks = _write_chunks(tmp_path, df)
    base = {"complete": True, "upsampled": False, "bar_size": "5 mins", "chunks": chunks}

    def check(manifest: dict[str, Any]) -> None:
        (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        m5.check_manifest(tmp_path, ("EURUSD",))

    check(base)
    with pytest.raises(ValueError, match="genuine"):
        check({**base, "upsampled": True})
    bad = [dict(chunks[0], sha256="0" * 64), chunks[1]]
    with pytest.raises(ValueError, match="SHA-256"):
        check({**base, "chunks": bad})
    bad = [dict(chunks[0], rows=1), chunks[1]]
    with pytest.raises(ValueError, match="row count"):
        check({**base, "chunks": bad})
    (tmp_path / "EURUSD" / "BID_M5_20240401.parquet").write_bytes(
        (tmp_path / "EURUSD" / "BID_M5_20240306.parquet").read_bytes()
    )
    with pytest.raises(ValueError, match="not in the manifest"):
        check(base)


def test_loader_rejects_non_native_m5_bars(tmp_path: Path) -> None:
    df = m5_frame(days=1)
    df.index = df.index + pd.Timedelta(minutes=2)  # not aligned to 5 minutes
    _write_chunks(tmp_path, df)
    with pytest.raises(ValueError, match="aligned"):
        m5.load_m5(tmp_path, "EURUSD")


def test_purge_spans_the_96_bar_label() -> None:
    df = m5_frame("2024-03-04", days=12)
    frames = {"EURUSD": df}
    feats = {"EURUSD": feature_frame(mid_of(df), "M5")}
    before = pd.Timestamp("2024-03-13", tz="UTC")
    p = replace(PROTOCOL, min_training_rows=10)
    _, y = training_rows(frames, feats, pd.Timestamp("2024-03-04", tz="UTC"), before, p)
    idx = pd.Series(df.index, index=df.index)
    end = idx.shift(-96) + m5.BAR
    ok = (end < before - pd.Timedelta(hours=24)) & ((end - idx) == 97 * m5.BAR)
    finite = (
        feats["EURUSD"][list(FEATURE_NAMES)].notna().all(axis=1) & feats["EURUSD"]["atr"].notna()
    )
    expected = int((ok & finite & mid_of(df)["close"].shift(-96).notna()).sum())
    assert len(y) == expected and expected > 0
