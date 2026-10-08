"""Dashboard services (no database): metric mapping, freshness, point-in-time views that agree
with the engines, deterministic explanations, research persistence and formal-test labels."""

from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.dashboard.services import analysis, explain, market, research
from app.dashboard.services.analysis import compute_engines
from app.validation.config import SliceSpec, ValidationConfig
from app.validation.engine import Validator
from tests.unit.strategies.helpers import session_walk

N = 460


@pytest.fixture(scope="module")
def bars() -> pd.DataFrame:
    b = session_walk(N, seed=13, start=date(2018, 1, 2))
    return b.assign(adj_close=b["close"] * 0.99)


@pytest.fixture(scope="module")
def bundle(bars: pd.DataFrame) -> analysis.EngineBundle:
    return compute_engines(bars)


# ── market ──


def test_market_overview_maps_the_stored_bars(bars: pd.DataFrame) -> None:
    ov = market.market_overview(bars)
    last, prev = bars.iloc[-1], bars.iloc[-2]
    assert (ov["open"], ov["high"], ov["low"], ov["close"], ov["volume"]) == (
        last["open"],
        last["high"],
        last["low"],
        last["close"],
        last["volume"],
    )
    assert ov["previous_close"] == prev["close"]
    assert ov["change"] == last["close"] - prev["close"]
    assert ov["change_pct"] == last["close"] / prev["close"] - 1
    assert ov["bars"] == N and ov["session"] == bars.index[-1].tz_convert("America/New_York").date()


@pytest.mark.parametrize(
    ("now", "stale", "missing", "status"),
    [
        # last bar 2024-07-03 (early close at 17:00 UTC); July 4th holiday
        ("2024-07-04 18:00", False, 0, "no regular session"),
        ("2024-07-05 15:00", False, 0, "in progress"),
        ("2024-07-05 20:30", True, 1, "has ended"),
        ("2024-07-08 12:00", True, 1, "before today's"),
    ],
)
def test_freshness_uses_the_exchange_calendar(
    now: str, stale: bool, missing: int, status: str
) -> None:
    last_bar = pd.Timestamp("2024-07-03 13:30", tz="UTC")
    f = market.freshness(last_bar, pd.Timestamp(now, tz="UTC").to_pydatetime())
    assert f.stale is stale and f.missing_sessions == missing
    assert status in f.market_status
    assert f.last_bar_session == date(2024, 7, 3)


def test_freshness_requires_aware_time() -> None:
    with pytest.raises(ValueError):
        market.freshness(pd.Timestamp("2024-07-03 13:30", tz="UTC"), datetime(2024, 7, 5))


def test_session_selection_is_explicit(bars: pd.DataFrame) -> None:
    sessions = market.session_dates(bars)
    assert market.session_on_or_before(sessions, sessions[10]) == sessions[10]
    sat = date(2018, 1, 6)  # a Saturday -> the Friday session, stated in the UI
    assert market.session_on_or_before(sessions, sat) == date(2018, 1, 5)
    with pytest.raises(market.NoDataError):
        market.session_on_or_before(sessions, date(2017, 1, 1))
    with pytest.raises(market.NoDataError):
        market.bar_ts_for(bars, sat)  # no nearest-date guessing for exact lookups


def test_dataset_identity_key_changes_with_the_data() -> None:
    t = pd.Timestamp("2024-01-02 14:30", tz="UTC")
    a = market.DatasetIdentity("SPY", 1, "yfinance", "1d", 10, t, t, t)
    b = market.DatasetIdentity("SPY", 1, "yfinance", "1d", 11, t, t, t)
    c = market.DatasetIdentity("SPY", 1, "yfinance", "1d", 10, t, t, t + pd.Timedelta(days=1))
    assert a.key == market.DatasetIdentity("SPY", 1, "yfinance", "1d", 10, t, t, t).key
    assert len({a.key, b.key, c.key}) == 3


# ── engine views agree with the engines ──


def test_candle_view_equals_engine_rows(bars: pd.DataFrame, bundle: analysis.EngineBundle) -> None:
    obs = bundle.candles.observations
    ts = obs["ts"].iloc[-1]
    v = analysis.candle_view(bundle, ts)
    assert [p["pattern"] for p in v["patterns"]] == obs.loc[obs["ts"] == ts, "pattern"].tolist()
    g = bundle.candles.geometry.loc[ts]
    assert v["geometry"]["body_ratio"] == g["body_ratio"]
    assert v["engine_version"] == bundle.candles.engine_version


def test_indicator_view_values_and_warm_up(
    bars: pd.DataFrame, bundle: analysis.EngineBundle
) -> None:
    early = analysis.indicator_view(bundle, bars.index[5]).set_index("indicator")
    assert early.loc["sma_200", "status"] == "warm-up" and pd.isna(early.loc["sma_200", "value"])
    late = analysis.indicator_view(bundle, bars.index[-1]).set_index("indicator")
    vals = bundle.indicators.values.iloc[-1]
    for col in ("sma_20", "rsi_14", "atr_14", "obv"):
        assert late.loc[col, "value"] == vals[col] and late.loc[col, "status"] == "ok"


def test_regime_view_previous_label_and_age(
    bars: pd.DataFrame, bundle: analysis.EngineBundle
) -> None:
    s = bundle.regimes.state
    ts = s.index[-1]
    v = analysis.regime_view(bundle, ts)
    labels = s["volatility_regime"].tolist()
    current = labels[-1]
    prev = next((x for x in reversed(labels) if x != current), None)
    assert v["dimensions"]["volatility"]["label"] == current
    assert v["dimensions"]["volatility"]["previous_label"] == prev
    assert v["dimensions"]["volatility"]["age_sessions"] == s["volatility_age"].iloc[-1]


def test_strategy_states_equal_engine_signals(
    bars: pd.DataFrame, bundle: analysis.EngineBundle
) -> None:
    ts = bars.index[-1]
    t = analysis.strategy_states(bundle, ts).set_index("strategy_id")
    sig = bundle.strategies.signals
    for sid in analysis.STRATEGIES:
        row = sig[(sig["strategy_id"] == sid) & (sig["bar_ts"] == ts)].iloc[0]
        assert t.loc[sid, "state"] == row["state"]
        assert t.loc[sid, "effective_at"] == row["effective_at"]
        assert t.loc[sid, "reason"] == row["reason"]


def test_price_action_views_are_point_in_time(
    bars: pd.DataFrame, bundle: analysis.EngineBundle
) -> None:
    """A view at T from the full history equals the view from a history ending at T: no pivot
    is shown before its confirmed_at and no event before its available_at."""
    cut = 380
    ts = bars.index[cut]
    prefix = compute_engines(bars.iloc[: cut + 1])
    full_v, pre_v = analysis.price_action_view(bundle, ts), analysis.price_action_view(prefix, ts)
    assert full_v["state"] == pre_v["state"]
    pd.testing.assert_frame_equal(full_v["recent_swings"], pre_v["recent_swings"])
    pd.testing.assert_frame_equal(full_v["recent_events"], pre_v["recent_events"])
    pd.testing.assert_frame_equal(
        analysis.swings_known_at(bundle, ts), analysis.swings_known_at(prefix, ts)
    )
    known = analysis.swings_known_at(bundle, ts)
    assert (known["confirmed_at"] <= ts).all()
    # confirmation latency: between a pivot and its confirmation the pivot is not shown
    sw = bundle.price_action.swings.iloc[-1]
    between = bars.index[int(sw["pivot_idx"]) + 1]
    assert between < sw["confirmed_at"]
    assert sw["swing_id"] not in set(analysis.swings_known_at(bundle, between)["swing_id"])
    assert sw["swing_id"] in set(analysis.swings_known_at(bundle, sw["confirmed_at"])["swing_id"])
    for name in ("candle_view", "regime_view"):
        assert getattr(analysis, name)(bundle, ts) == getattr(analysis, name)(prefix, ts)
    pd.testing.assert_frame_equal(
        analysis.strategy_states(bundle, ts), analysis.strategy_states(prefix, ts)
    )


# ── explanation ──


def _explain(bundle: analysis.EngineBundle, ts: pd.Timestamp, risk=None):  # type: ignore[no-untyped-def]
    return explain.explain_session(
        session=ts.date(),
        price={"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "change_pct": None},
        candle=analysis.candle_view(bundle, ts),
        price_action=analysis.price_action_view(bundle, ts),
        regime=analysis.regime_view(bundle, ts),
        strategy=None,
        risk=risk,
        paper=None,
    )


def test_explanation_is_deterministic_traceable_and_neutral(bundle: analysis.EngineBundle) -> None:
    ts = bundle.regimes.state.index[-1]
    a, b = _explain(bundle, ts), _explain(bundle, ts)
    assert a == b
    assert all(line.sources for line in a)
    text = " ".join(line.text.lower() for line in a)
    for word in ("buy", "sell", "should", "recommend", "will rise", "will fall", "guarantee"):
        assert word not in text
    assert "change vs previous close undefined" in a[0].text  # undefined stays undefined
    regime = analysis.regime_view(bundle, ts)
    assert regime["dimensions"]["trend"]["label"] in next(x.text for x in a if x.section == "Trend")


# ── research ──


def test_formal_test_status_labels_are_exact() -> None:
    comps = pd.DataFrame(
        {
            "family": ["formal", "formal", "formal", "descriptive"],
            "a": ["sma_trend", "rsi_momentum", "regime_trend", "x"],
            "b": [research.cmp.PRICE_BENCH] * 3 + ["y"],
            "metric": ["sharpe"] * 4,
            "estimate": [0.1, -0.2, 0.3, 1.0],
            "p_holm": [0.2, 0.01, np.nan, np.nan],
        }
    )
    f = research.classify_formal(comps)
    assert f["status"].tolist() == [
        "not significant at 0.05",
        "significant at 0.05 (Holm)",
        "undefined (insufficient evidence)",
    ]
    assert research.validation_conclusion(comps) == "NO STATISTICAL EDGE FOUND"  # negative only
    comps.loc[0, "p_holm"] = 0.001
    assert research.validation_conclusion(comps).startswith("Holm-significant positive")


def test_validation_persistence_round_trip(bars: pd.DataFrame, tmp_path: Path) -> None:
    cfg = ValidationConfig(
        slices=(SliceSpec("full"),), formal_slice="full", n_resamples=100, block_length=5
    )
    with pytest.raises(research.ResultsMissingError):
        research.load_validation(tmp_path, "k", cfg)
    research.compute_validation(bars, tmp_path, "k", cfg)
    frames, meta = research.load_validation(tmp_path, "k", cfg)
    ref = Validator(cfg).run(bars).frames()
    assert meta["config_fingerprint"] == cfg.fingerprint() and meta["dataset_key"] == "k"
    for name in ("intervals", "comparisons", "cost_sensitivity"):
        num = ref[name].select_dtypes("number").columns
        np.testing.assert_allclose(
            frames[name][num].to_numpy(float),
            ref[name][num].to_numpy(float),
            rtol=1e-12,
            equal_nan=True,
        )
    with pytest.raises(research.ResultsMissingError):
        research.load_validation(tmp_path, "other-dataset", cfg)


def test_calibration_table() -> None:
    p = np.linspace(0.4, 0.6, 200)
    preds = pd.DataFrame(
        {"period": "test", "target": (np.arange(200) % 2).astype(float), "p_lr": p}
    )
    t = research.calibration_table(preds, "lr", bins=4)
    assert t["n"].sum() == 200 and len(t) == 4
    assert t["mean_predicted"].is_monotonic_increasing
    assert research.calibration_table(preds.assign(period="train"), "lr").empty


def test_backtest_table_matches_phase8(bars: pd.DataFrame) -> None:
    from app.backtest.engine import run_suite

    out = research.backtest_suite(bars)
    ref = run_suite(bars)
    for sid, r in ref.items():
        for col in research.BACKTEST_COLUMNS:
            a, b = out["table"].loc[sid, col], r.metrics[col]
            assert (a == b) or (pd.isna(a) and pd.isna(b)), (sid, col)


def test_time_is_never_naive() -> None:
    f = market.freshness(pd.Timestamp("2024-07-03 13:30", tz="UTC"), datetime.now(UTC))
    assert f.now.tzinfo is not None
