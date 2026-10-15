"""Presentation and traceability of the risk research statistics: exploratory interval labels,
bootstrap diagnostics, manifest environment and sparse-event warnings.

Synthetic return series and fake suite runs only: no backtest, no database."""

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.risk import cli, diagnostics, research
from app.risk.config import SCENARIOS, DrawdownSpec, RiskConfig, SessionSpec, StopSpec
from app.risk.diagnostics import METRIC_KEYS, RiskSuiteReport
from app.risk.engine import RiskRun
from app.validation.config import ValidationConfig
from app.validation.metrics import matrix_metrics
from app.validation.resampling import bootstrap_summary, percentile_interval
from tests.unit.risk.test_research_cli import SELECT, fake_equity, make_bars

ORIGINAL_COLUMNS = [
    "slice", "strategy", "scenario", "vs", "metric", "estimate", "lower", "upper",
    "family", "block_length", "n_resamples", "seed",
]  # fmt: skip
NEW_COLUMNS = [
    "bootstrap_median", "n_finite", "order_dependent", "interval_kind",
    "estimate_in_interval", "note",
]  # fmt: skip
SMALL = ValidationConfig(n_resamples=200)


def run_with(
    returns: np.ndarray, scenario: str = "x", diag: dict[str, float] | None = None
) -> RiskRun:
    idx = pd.date_range("2020-01-02", periods=len(returns), freq="D", tz="UTC")
    equity = fake_equity(idx, list(returns))
    metrics = {k: 0.0 for k in METRIC_KEYS}
    empty = pd.DataFrame()
    return RiskRun("s", scenario, empty, equity, empty, empty, None, None, metrics, diag or {})


def synthetic_report(overlay_returns: dict[str, np.ndarray]) -> RiskSuiteReport:
    rng = np.random.default_rng(3)
    base = rng.normal(0.0004, 0.01, 600)
    report = RiskSuiteReport("1.0.0", "bt", {})
    report.runs[("full", "s", "control_no_overlay")] = run_with(base)
    for name, r in overlay_returns.items():
        report.runs[("full", "s", name)] = run_with(r)
    return report


# ── bootstrap diagnostics ──


def test_bootstrap_summary_matches_percentile_interval_and_adds_median() -> None:
    x = np.random.default_rng(0).normal(size=1000)
    bs = bootstrap_summary(x, 0.95)
    assert (bs.lower, bs.upper, bs.n_finite) == percentile_interval(x, 0.95)
    assert bs.median == pytest.approx(float(np.median(x))) and bs.n_total == 1000


def test_bootstrap_summary_excludes_non_finite_explicitly() -> None:
    x = np.array([1.0, 2.0, np.nan, 3.0, np.inf, -np.inf, 4.0])
    bs = bootstrap_summary(x, 0.5)
    assert bs.n_finite == 4 and bs.n_total == 7
    assert bs.median == 2.5
    assert (bs.lower, bs.upper) == pytest.approx((1.75, 3.25))


def test_bootstrap_summary_with_no_finite_sample() -> None:
    bs = bootstrap_summary(np.array([np.nan, np.inf]), 0.95)
    assert bs.n_finite == 0 and bs.n_total == 2
    assert all(np.isnan(v) for v in (bs.lower, bs.upper, bs.median))


# ── comparison rows: labels, diagnostics, compatibility ──


def test_comparison_rows_keep_existing_columns_and_append_diagnostics() -> None:
    rng = np.random.default_rng(5)
    report = synthetic_report({"fixed_fraction_50": rng.normal(0.0002, 0.005, 600)})
    rows = diagnostics._compare(report, "full", ["control_no_overlay", "fixed_fraction_50"], SMALL)
    assert len(rows) == len(diagnostics.COMPARED_METRICS)
    for row in rows:
        assert list(row)[: len(ORIGINAL_COLUMNS)] == ORIGINAL_COLUMNS  # unchanged prefix
        assert list(row)[len(ORIGINAL_COLUMNS) :] == NEW_COLUMNS
        assert row["family"] == "descriptive"
        assert row["interval_kind"] == diagnostics.INTERVAL_KIND
        assert row["n_finite"] == SMALL.n_resamples
        assert row["lower"] <= row["bootstrap_median"] <= row["upper"]
        assert row["estimate_in_interval"] == (row["lower"] <= row["estimate"] <= row["upper"])
        dependent = row["metric"] == "max_drawdown"
        assert row["order_dependent"] is dependent
        assert (row["note"] == diagnostics.ORDER_DEPENDENT_NOTE) is dependent
        assert (row["note"] == "") is not dependent


def test_undefined_estimates_are_flagged_not_hidden() -> None:
    bad = np.full(600, np.nan)
    report = synthetic_report({"fixed_fraction_50": bad})
    rows = diagnostics._compare(report, "full", ["control_no_overlay", "fixed_fraction_50"], SMALL)
    for row in rows:
        assert row["n_finite"] == 0 and np.isnan(row["bootstrap_median"])
        assert np.isnan(row["estimate"]) and row["estimate_in_interval"] is None


def test_order_dependent_metrics_are_exactly_those_changed_by_reordering() -> None:
    r = np.random.default_rng(11).normal(0.0004, 0.01, (2000, 1))
    p = np.random.default_rng(12).permutation(2000)
    a, b = matrix_metrics(r, 252), matrix_metrics(r[p], 252)
    changed = {m for m in diagnostics.COMPARED_METRICS if not np.isclose(a[m][0], b[m][0])}
    assert changed == set(diagnostics.ORDER_DEPENDENT_METRICS) == {"max_drawdown"}


# ── sparse intervention events ──


@pytest.mark.parametrize(
    ("count", "level"),
    [(0.0, "none"), (1.0, "single"), (2.0, "few"), (4.0, "few"), (5.0, "several"),
     (40.0, "several"), (None, "not_counted"), (float("nan"), "not_counted")],
)  # fmt: skip
def test_event_levels(count: float | None, level: str) -> None:
    assert research.event_level(count, 5) == level


def test_event_threshold_is_configurable_and_validated() -> None:
    assert research.event_level(2.0, 2) == "several"
    assert research.event_level(2.0, 3) == "few"
    with pytest.raises(ValueError):
        research.event_level(1.0, 0)


def test_event_controls_follow_the_effective_configuration() -> None:
    controls = {s.name: research.event_controls(s) for s in SCENARIOS}
    assert controls["control_no_overlay"] == [] and controls["fixed_fraction_50"] == []
    assert controls["drawdown_lock_20"] == [("drawdown_lock", "drawdown_lock_events")]
    # ADR-0034 D-A1/D-A8: a stop event is an ACTIVATION (one per position episode)
    assert controls["atr_stop_3"] == [("stop", "stop_activations")]
    session = RiskConfig("s", session=SessionSpec(enabled=True))
    assert research.event_controls(session) == [("session_lock", "session_lock_events")]
    assert controls["volatility_target_10"] == [] and controls["atr_risk_1pct"] == []


def test_event_rows_and_table_columns_for_zero_one_and_several_events() -> None:
    both = RiskConfig("both", stop=StopSpec("atr"), drawdown=DrawdownSpec(enabled=True))
    scen = (SCENARIOS[0], SCENARIOS[3], SCENARIOS[4], both)  # control, dd20, atr3, combined
    report = RiskSuiteReport("1.0.0", "bt", {})
    r = np.zeros(10)
    report.runs[("full", "s", "control_no_overlay")] = run_with(r)
    report.runs[("full", "s", "drawdown_lock_20")] = run_with(r, diag={"drawdown_lock_events": 0})
    report.runs[("full", "s", "atr_stop_3")] = run_with(r, diag={"stop_activations": 1})
    report.runs[("full", "s", "both")] = run_with(
        r, diag={"stop_activations": 7, "drawdown_lock_events": 3}
    )
    events = research.intervention_events(report, scen, 5)
    levels = {(e["scenario"], e["control"]): e["level"] for e in events}
    assert levels == {
        ("control_no_overlay", None): "not_applicable",
        ("drawdown_lock_20", "drawdown_lock"): "none",
        ("atr_stop_3", "stop"): "single",
        ("both", "stop"): "several",
        ("both", "drawdown_lock"): "few",
    }
    table = pd.DataFrame(
        [{"slice": "full", "strategy": "s", "scenario": k[2]} for k in report.runs]
    )
    out = research.with_event_columns(table, events).set_index("scenario")
    lv, cnt, warn = (
        {str(k): str(v) for k, v in out[c].items()}
        for c in ("event_levels", "event_counts", "event_warning")
    )
    assert lv["control_no_overlay"] == "not_applicable" and warn["control_no_overlay"] == ""
    assert cnt["drawdown_lock_20"] == "drawdown_lock_events=0"
    assert "not evidence that it is ineffective" in warn["drawdown_lock_20"]
    assert "single episode" in warn["atr_stop_3"]
    assert lv["both"] == "stop:several;drawdown_lock:few"
    assert warn["both"].startswith("drawdown_lock: few events")


# ── manifest (CLI path with a fake suite) ──


class EventSuite:
    """Fake suite: exactly the requested runs, with event counters and real comparison rows."""

    def __call__(self, bars: pd.DataFrame, **kw: Any) -> RiskSuiteReport:
        scen = kw["scenarios"]
        report = RiskSuiteReport("1.0.0", kw["backtest"].fingerprint(), {})
        counts: dict[str, dict[str, float]] = {
            "drawdown_lock_20": {"drawdown_lock_events": 4.0},
            "atr_stop_3": {"stop_activations": 0.0},
        }
        rng = np.random.default_rng(2)
        for sl in kw["slices"]:
            for sid in [s.strategy_id for s in kw["strategy_engine"].strategies]:
                for sc in scen:
                    report.runs[(sl.name, sid, sc.name)] = run_with(
                        rng.normal(0.0003, 0.01, 300), sc.name, counts.get(sc.name, {})
                    )
            if kw["compare"]:
                report.comparisons += diagnostics._compare(
                    report, sl.name, [s.name for s in scen], SMALL
                )
        return report


@pytest.fixture
def complete_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    code = {"app_version": "0", "git_commit": "c" * 40, "git_branch": "b", "git_dirty": False}
    monkeypatch.setattr(research, "code_info", lambda: dict(code))
    out = tmp_path / "r"
    rc = cli.main(["run", "--symbol", "SPY", "--out", str(out), *SELECT],
                  load_bars=make_bars, run_suite=EventSuite(), configure=False)  # fmt: skip
    assert rc == cli.EXIT_OK
    return out


def _manifest(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((path / research.MANIFEST).read_text(encoding="utf-8"))
    return data


def test_manifest_records_environment_statistics_and_events(complete_run: Path) -> None:
    m = _manifest(complete_run)
    # v2 fields are still present in the current (v3) schema
    assert m["schema_version"] == "risk-research-run/3" and m["status"] == research.COMPLETE
    assert m["environment"] == {"numpy_version": np.__version__}
    comp = m["config"]["comparisons"]
    assert comp["validation_config_fingerprint"] == ValidationConfig().fingerprint()
    assert comp["family"] == "descriptive" and comp["interval_kind"] == diagnostics.INTERVAL_KIND
    assert comp["order_dependent_metrics"] == ["max_drawdown"]
    stats = m["statistics"]
    assert "NOT validated confidence intervals" in stats["intervals"]
    assert stats["order_dependent_metrics"] == {"max_drawdown": diagnostics.ORDER_DEPENDENT_NOTE}
    assert stats["formal_tests"].startswith("none") and stats["sparse_event_threshold"] == 5
    ev = {(e["scenario"], e["control"]): (e["count"], e["level"])
          for e in m["results"]["intervention_events"]}  # fmt: skip
    assert ev[("drawdown_lock_20", "drawdown_lock")] == (4.0, "few")
    assert ev[("atr_stop_3", "stop")] == (0.0, "none")
    assert ev[("fixed_fraction_50", None)] == (None, "not_applicable")
    outside = m["results"]["estimates_outside_own_interval"]
    assert all(set(o) == {"slice", "strategy", "scenario", "metric"} for o in outside)


def test_artifacts_carry_the_new_columns(complete_run: Path) -> None:
    table = pd.read_csv(complete_run / research.TABLE, keep_default_na=False)
    assert list(table.columns)[-3:] == ["event_levels", "event_counts", "event_warning"]
    comp = pd.read_csv(complete_run / research.COMPARISONS)
    assert list(comp.columns) == ORIGINAL_COLUMNS + NEW_COLUMNS
    mdd = comp[comp["metric"] == "max_drawdown"]
    assert (
        mdd["order_dependent"].all()
        and not comp[comp["metric"] != "max_drawdown"]["order_dependent"].any()
    )


def test_validation_fingerprint_is_deterministic_and_sensitive() -> None:
    assert ValidationConfig().fingerprint() == ValidationConfig().fingerprint()
    assert ValidationConfig(seed=1).fingerprint() != ValidationConfig().fingerprint()


def test_invalid_sparse_event_threshold_is_refused(tmp_path: Path) -> None:
    sel = research.resolve_selection(strategies=["sma_trend"], scenarios=["control_no_overlay"],
                                     cost="D", slices=["full"], compare=False)  # fmt: skip
    with pytest.raises(ValueError):
        research.execute(sel, symbol="SPY", run_dir=tmp_path, run_id="r", load_bars=make_bars,
                         sparse_event_threshold=0)  # fmt: skip
