"""Validator: provenance, determinism, Phase 8 equivalence, cost scenarios, slicing and
point-in-time guarantees (prefix/append, future OHLC/volume/adj_close/state mutation, cost
independence, slice-bounded resampling, leaky-slice control)."""

import dataclasses
import math
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.backtest.config import BacktestConfig
from app.backtest.engine import Backtester
from app.validation.config import COST_SCENARIOS, CostScenario, SliceSpec, ValidationConfig
from app.validation.engine import Validator
from app.validation.models import ValidationReport
from tests.unit.strategies.helpers import session_walk, small_engine

N = 260
BARS_CACHE: dict[int, pd.DataFrame] = {}


def walk(n: int = N, seed: int = 1) -> pd.DataFrame:
    if (n, seed) not in BARS_CACHE:
        b = session_walk(n, seed)
        BARS_CACHE[(n, seed)] = b.assign(adj_close=b["close"] * np.linspace(0.9, 1.0, n))  # type: ignore[index]
    return BARS_CACHE[(n, seed)].copy()  # type: ignore[index]


SPLIT = session_walk(N, 1).index[150].date()  # fixed calendar dates for the synthetic series
CFG = ValidationConfig(
    n_resamples=200,
    block_length=10,
    slices=(SliceSpec("full"), SliceSpec("early", end=SPLIT), SliceSpec("late", start=SPLIT)),
)


def validate(bars: pd.DataFrame, cfg: ValidationConfig = CFG) -> ValidationReport:
    return Validator(cfg, strategy_engine=small_engine()).run(bars)


def slice_frames(rep: ValidationReport, name: str) -> dict[str, pd.DataFrame]:
    f = rep.frames()
    return {
        "intervals": f["intervals"][f["intervals"]["prov_slice_name"] == name].reset_index(
            drop=True
        ),
        "comparisons": f["comparisons"][f["comparisons"]["prov_slice_name"] == name][
            ["comparison_id", "estimate", "lower", "upper", "p_value"]
        ].reset_index(drop=True),
        "costs": f["cost_sensitivity"][f["cost_sensitivity"]["slice_name"] == name].reset_index(
            drop=True
        ),
    }


@pytest.fixture(scope="module")
def report() -> ValidationReport:
    return validate(walk())


# ── structure / provenance ──


def test_formal_family_and_corrections(report: ValidationReport) -> None:
    c = report.frames()["comparisons"]
    formal = c[c["family"] == "formal"]
    assert len(formal) == 30
    assert set(formal["prov_slice_name"]) == {"full"} and set(formal["family_size"]) == {30}
    valid = formal[formal["p_value"].notna()]
    assert valid["p_value"].between(0, 1).all() and (valid["p_holm"] >= valid["p_value"]).all()
    assert (valid["p_bh"] <= valid["p_holm"] + 1e-12).all()
    undefined = formal[formal["p_value"].isna()]  # e.g. zero-variance series on synthetic data
    assert undefined["p_holm"].isna().all() and undefined["note"].str.contains("UNDEFINED").all()
    desc = c[c["family"] == "descriptive"]
    assert desc["p_value"].isna().all() and desc["p_holm"].isna().all()


def test_every_result_carries_provenance(report: ValidationReport) -> None:
    for row in report.intervals + report.comparisons:
        p = row.provenance
        assert (p.seed, p.block_length, p.n_resamples, p.confidence) == (CFG.seed, 10, 200, 0.95)
        assert p.method == "moving_block" and p.config_fingerprint == CFG.fingerprint()
        assert p.cost_scenario == "D_default" and len(p.backtest_fingerprint) == 16
        assert p.slice_name in {"full", "early", "late"}


def test_point_estimates_equal_phase8_metrics(report: ValidationReport) -> None:
    bars = walk()
    run = small_engine().run(bars.drop(columns=["adj_close"]))
    res = Backtester(BacktestConfig(period_label="full")).run(bars, run)
    iv = report.frames()["intervals"]
    iv = iv[iv["prov_slice_name"] == "full"].set_index(["subject", "metric"])
    for sid, r in res.items():
        for m in ("cumulative_return", "cagr", "sharpe", "max_drawdown"):
            got, ref = iv.loc[(sid, m), "point"], r.metrics[m]
            assert (math.isnan(got) and math.isnan(ref)) or got == pytest.approx(
                ref, rel=1e-9, abs=1e-12
            )


def test_deterministic_and_seed_sensitive() -> None:
    a, b = validate(walk()), validate(walk())
    for k, frame in a.frames().items():
        pd.testing.assert_frame_equal(frame, b.frames()[k], obj=k)
    other = validate(walk(), dataclasses.replace(CFG, seed=CFG.seed + 1))
    assert not a.frames()["intervals"]["lower"].equals(other.frames()["intervals"]["lower"])
    assert other.config_fingerprint != a.config_fingerprint


# ── cost sensitivity ──


def test_cost_scenarios(report: ValidationReport) -> None:
    cs = report.frames()["cost_sensitivity"]
    full = cs[cs["slice_name"] == "full"]
    assert set(full["scenario"]) == {"A_zero", "B_5bps", "C_10bps", "D_default"}
    pivot = full.pivot(index="subject", columns="scenario", values="cumulative_return")
    assert (pivot["A_zero"] >= pivot["B_5bps"] - 1e-12).all()
    assert (pivot["B_5bps"] >= pivot["C_10bps"] - 1e-12).all()
    assert (full[full["scenario"] == "A_zero"]["total_costs"] == 0).all()
    orders = full.pivot(index="subject", columns="scenario", values="orders")
    assert (orders.nunique(axis=1) == 1).all()  # costs never change states or orders


def test_default_scenario_reproduces_phase8_default(report: ValidationReport) -> None:
    bars = walk()
    run = small_engine().run(bars.drop(columns=["adj_close"]))
    res = Backtester(BacktestConfig(period_label="full")).run(bars, run)
    cs = report.frames()["cost_sensitivity"]
    d = cs[(cs["slice_name"] == "full") & (cs["scenario"] == "D_default")].set_index("subject")
    for sid, r in res.items():
        assert d.loc[sid, "total_costs"] == pytest.approx(r.metrics["total_costs"])
        assert d.loc[sid, "cumulative_return"] == pytest.approx(r.metrics["cumulative_return"])


def test_extra_scenario_does_not_change_other_results(report: ValidationReport) -> None:
    extra = dataclasses.replace(
        CFG, cost_scenarios=(*COST_SCENARIOS, CostScenario("E_50bps", 50.0, 0.0))
    )
    rep = validate(walk(), extra)
    for name in ("full", "early"):
        x, y = slice_frames(report, name), slice_frames(rep, name)
        pd.testing.assert_frame_equal(x["comparisons"], y["comparisons"])
        pd.testing.assert_frame_equal(
            x["intervals"].drop(columns=["prov_config_fingerprint"]),
            y["intervals"].drop(columns=["prov_config_fingerprint"]),
        )


# ── slicing ──


def test_slices_cover_their_dates_and_bootstrap_stays_inside(report: ValidationReport) -> None:
    iv = report.frames()["intervals"]
    bars = walk()
    for name, first, last in (("full", 0, N - 1), ("early", 0, 150), ("late", 150, N - 1)):
        p = iv[iv["prov_slice_name"] == name].iloc[0]
        assert p["prov_slice_first_session"] == bars.index[first]
        assert p["prov_slice_last_session"] == bars.index[last]
        assert p["prov_sessions"] == last - first + 1


def test_block_length_longer_than_slice_is_rejected() -> None:
    cfg = dataclasses.replace(CFG, block_length=500)
    with pytest.raises(ValueError, match="block_length"):
        validate(walk(), cfg)


# ── point-in-time / leakage ──


def test_closed_slice_unchanged_when_data_is_appended() -> None:
    short = validate(walk().iloc[:200])
    full = validate(walk())
    x, y = slice_frames(short, "early"), slice_frames(full, "early")
    for k in x:
        pd.testing.assert_frame_equal(x[k], y[k], obj=k)


@pytest.mark.parametrize("columns", [["open", "high", "low", "close"], ["volume"], ["adj_close"]])
def test_future_mutation_does_not_change_closed_slice(columns: list[str]) -> None:
    base = validate(walk())
    altered = walk()
    rng = np.random.default_rng(9)
    future = altered.index > pd.Timestamp(SPLIT).tz_localize("UTC") + pd.Timedelta(days=1)
    for col in columns:
        altered.loc[future, col] = altered.loc[future, col].to_numpy() * rng.uniform(
            0.5, 1.5, future.sum()
        )
    altered["high"] = altered[["open", "high", "low", "close"]].max(axis=1)
    altered["low"] = altered[["open", "high", "low", "close"]].min(axis=1)
    mutated = validate(altered)
    x, y = slice_frames(base, "early"), slice_frames(mutated, "early")
    for k in x:
        pd.testing.assert_frame_equal(x[k], y[k], obj=k)


def test_future_state_mutation_does_not_change_slice_statistics() -> None:
    bars = walk()
    run = small_engine().run(bars.drop(columns=["adj_close"]))
    sig = run.signals[run.signals["strategy_id"] == "sma_trend"].set_index("bar_ts")
    states = sig["state"].copy()
    mutated = states.copy()
    mutated.iloc[160:] = np.where(mutated.iloc[160:] == "LONG", "FLAT", "LONG")
    bt = Backtester(BacktestConfig(end=SPLIT))
    a = bt.run_states(bars, states, strategy_id="x")
    b = bt.run_states(bars, mutated, strategy_id="x")
    v = Validator(CFG)
    ra = v.analyze_returns(
        a.equity[["daily_return"]], slice_name="early", cost_scenario="D", backtest_fingerprint="f"
    )
    rb = v.analyze_returns(
        b.equity[["daily_return"]], slice_name="early", cost_scenario="D", backtest_fingerprint="f"
    )
    assert ra == rb


def test_inputs_are_never_modified() -> None:
    bars = walk()
    before = bars.copy(deep=True)
    validate(bars)
    pd.testing.assert_frame_equal(bars, before)


def test_leaky_slice_end_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: a validator that ignores the slice end (reads future bars) must change the
    'early' slice when data is appended — the append check above would catch it."""

    def leaky_strategy_run(self: Validator, bars: pd.DataFrame, end: date | None):  # type: ignore[no-untyped-def]
        features = bars.drop(columns=["adj_close"])
        return bars, self.strategy_engine.run(features)  # ignores `end`

    def leaky_backtest(self, upto, run, sl, scenario):  # type: ignore[no-untyped-def]
        cfg = dataclasses.replace(
            self.backtest_config, start=sl.start, end=None, period_label=sl.name
        )
        bt = Backtester(cfg)
        return {**bt.run(upto, run), **bt.benchmarks(upto)}, cfg

    monkeypatch.setattr(Validator, "_strategy_run", leaky_strategy_run)
    monkeypatch.setattr(Validator, "_backtest", leaky_backtest)
    short, full = validate(walk().iloc[:200]), validate(walk())
    x, y = slice_frames(short, "early"), slice_frames(full, "early")
    assert not x["intervals"]["point"].equals(y["intervals"]["point"])


# ── configuration ──


@pytest.mark.parametrize(
    "kw",
    [{"block_length": 0}, {"n_resamples": 10}, {"confidence": 1.0}, {"method": "iid"},
     {"slices": ()}, {"default_scenario": "Z"}, {"formal_slice": "nope"},
     {"cost_scenarios": (CostScenario("x", -1.0, 0.0),)}],
)  # fmt: skip
def test_invalid_config(kw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        ValidationConfig(**kw)  # type: ignore[arg-type]


def test_d_default_matches_phase8_cost_model() -> None:
    d = next(c for c in COST_SCENARIOS if c.name == "D_default")
    bt = BacktestConfig()
    o = d.backtest_overrides()
    assert (o["slippage_bps"], o["spread_bps"], o["commission_per_trade"]) == (
        bt.slippage_bps, bt.spread_bps, bt.commission_per_trade,
    )  # fmt: skip


def test_redundancy_ignores_floating_point_noise() -> None:
    from app.validation.engine import _redundancy

    class _R:
        def __init__(self, eq: pd.DataFrame) -> None:
            self.equity = eq

    idx = pd.RangeIndex(4)
    a = pd.DataFrame({"position": [0, 1, 1, 0], "daily_return": [0.0, 0.01, 0.02, 0.0]}, index=idx)
    b = a.assign(daily_return=a["daily_return"] + np.array([0, 1e-16, -1e-16, 0]))
    row = _redundancy("s", {"a": _R(a), "b": _R(b)}, "a", "b")  # type: ignore[dict-item]
    assert row.sessions_with_different_returns == 0 and row.position_agreement == 1.0
    c = a.assign(daily_return=a["daily_return"] + np.array([0, 0, 1e-6, 0]))
    assert _redundancy("s", {"a": _R(a), "c": _R(c)}, "a", "c").sessions_with_different_returns == 1  # type: ignore[dict-item]
