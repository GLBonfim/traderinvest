"""ADR-0034 step 4b.1: `<metric>_status` columns of `risk_table.csv`.

An empty cell of a return-based metric can mean two different things: the series has undefined
returns (None) or the metric is NaN in its own domain (fewer than 2 sessions, zero standard
deviation, no downside). The status columns say which, from the calculation layer; zero stays
a defined value. Synthetic data, fake suites and `tmp_path` only.
"""

import math
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.backtest.metrics import (
    METRIC_DEFINED,
    METRIC_UNDEFINED_NAN,
    METRIC_UNDEFINED_RETURNS,
    RETURN_BASED_METRICS,
    return_metric_status,
    without_undefined_return_metrics,
)
from app.risk import artifacts as art
from app.risk import research
from app.risk.diagnostics import METRIC_KEYS, RiskSuiteReport
from app.risk.engine import RiskRun
from tests.unit.risk.test_adr0034_baseline import CONTROL, F, L
from tests.unit.risk.test_da5_invalid_state import BLOCK3, total_loss_bars
from tests.unit.risk.test_overlay import make_bars, run
from tests.unit.risk.test_research_artifacts import (
    GridSuite,
    _fixed_code,  # noqa: F401 (autouse fixture: fixed code version in manifests)
    manifest,
    run_grid,
)
from tests.unit.risk.test_undefined_returns import ZeroSuite

STATUS = [f"{k}_status" for k in RETURN_BASED_METRICS]


def metrics(**kw: Any) -> dict[str, Any]:
    return {"annualized_volatility": 0.1, "sharpe": 0.5, "sortino": 0.7, **kw}


# ── the three states, from the calculation layer ──


def test_finite_values_including_zero_are_defined() -> None:
    got = return_metric_status(metrics(sharpe=0.0, sortino=-1.2))
    assert got == dict.fromkeys(STATUS, METRIC_DEFINED)


def test_nan_is_undefined_nan_per_metric() -> None:
    got = return_metric_status(metrics(sortino=math.nan))
    assert got["sortino_status"] == METRIC_UNDEFINED_NAN
    assert got["sharpe_status"] == got["annualized_volatility_status"] == METRIC_DEFINED


def test_none_from_undefined_returns_is_undefined_returns() -> None:
    m = without_undefined_return_metrics(metrics(), 2, 0)
    assert return_metric_status(m) == dict.fromkeys(STATUS, METRIC_UNDEFINED_RETURNS)


@pytest.mark.parametrize(
    "m",
    [
        metrics(sharpe=None),  # None without undefined returns
        {**metrics(), "undefined_returns": 2},  # computed although undefined returns exist
        metrics(sharpe=math.inf),  # never produced by the calculation layer
    ],
    ids=["none_without_cause", "number_with_undefined", "infinite"],
)
def test_states_the_calculation_never_produces_are_refused(m: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        return_metric_status(m)


# ── real engine runs ──


def test_engine_run_with_undefined_returns() -> None:
    r = run(total_loss_bars(), [L] * len(total_loss_bars()), BLOCK3)
    assert return_metric_status(r.metrics) == dict.fromkeys(STATUS, METRIC_UNDEFINED_RETURNS)


def test_engine_run_without_downside_has_undefined_nan_sortino_only() -> None:
    bars = make_bars([(100 + i, 101 + i, 99 + i, 100 + i) for i in range(8)])
    r = run(bars, [L] * 8, CONTROL)  # held through rising closes: no negative return
    got = return_metric_status(r.metrics)
    assert got["sortino_status"] == METRIC_UNDEFINED_NAN and math.isnan(r.metrics["sortino"])
    assert got["sharpe_status"] == METRIC_DEFINED


def test_engine_run_always_flat_keeps_zero_distinct_from_nan() -> None:
    bars = make_bars([(100, 101, 99, 100)] * 6)
    r = run(bars, [F] * 6, CONTROL)  # every return is exactly 0
    assert r.metrics["annualized_volatility"] == 0.0
    got = return_metric_status(r.metrics)
    assert got["annualized_volatility_status"] == METRIC_DEFINED  # a defined zero
    assert got["sharpe_status"] == METRIC_UNDEFINED_NAN  # std 0: NaN, not 0


def test_engine_run_with_one_session_is_undefined_nan() -> None:
    r = run(make_bars([(100, 101, 99, 100)]), [F], CONTROL)
    assert return_metric_status(r.metrics) == dict.fromkeys(STATUS, METRIC_UNDEFINED_NAN)


# ── the table and its CSV ──


def old_table(report: RiskSuiteReport) -> pd.DataFrame:
    """The table as built before step 4b.1 (no status columns)."""
    return pd.DataFrame(
        [
            {"slice": s, "strategy": st, "scenario": sc,
             **{k: r.metrics[k] for k in METRIC_KEYS}, **r.diagnostics}
            for (s, st, sc), r in report.runs.items()
        ]
    )  # fmt: skip


class MixedSuite(ZeroSuite):
    """ZeroSuite plus consistent metrics: undefined returns on (full, sma_trend, atr_stop_3),
    a legitimate NaN Sharpe on (full, buy_and_hold, control_no_overlay), zero elsewhere."""

    def __call__(self, bars: pd.DataFrame, **kw: Any) -> RiskSuiteReport:
        report = super().__call__(bars, **kw)
        for key, r in list(report.runs.items()):
            m = dict(r.metrics)
            if key == ("full", "sma_trend", "atr_stop_3"):
                m = without_undefined_return_metrics(m, 3, 0)
            elif key == ("full", "buy_and_hold", "control_no_overlay"):
                m["sharpe"] = math.nan
            report.runs[key] = RiskRun(r.strategy_id, r.scenario, r.decisions, r.equity,
                                       r.fills, r.trades, None, None, m, r.diagnostics,
                                       r.risk_fingerprint, r.backtest_fingerprint)  # fmt: skip
        return report


def test_report_table_adds_status_columns_and_keeps_every_other_value() -> None:
    bars = make_bars([(100, 101, 99, 100)] * 6)
    rising = make_bars([(100, 101, 99, 100 + i * (-1) ** i) for i in range(8)])
    report = RiskSuiteReport("1.0.0", "bt", {})
    for i, (bars_, states, risk) in enumerate(
        [
            (total_loss_bars(), [L] * 11, BLOCK3),
            (bars, [F] * 6, CONTROL),
            (rising, [L] * 8, CONTROL),
        ]
    ):
        report.runs[("full", f"s{i}", risk.name)] = run(bars_, states, risk)
    new, old = report.table(), old_table(report)
    assert list(new.columns) == list(old.columns) + STATUS  # appended, nothing moved
    pd.testing.assert_frame_equal(new[list(old.columns)], old)  # same values (None/NaN/0)
    assert new["sharpe_status"].tolist() == [
        METRIC_UNDEFINED_RETURNS,
        METRIC_UNDEFINED_NAN,
        METRIC_DEFINED,
    ]


def test_csv_tells_undefined_returns_from_nan_and_zero(tmp_path: Path) -> None:
    rc, out = run_grid(tmp_path, suite=MixedSuite())
    assert rc == 0 and art.verify_run(out).ok
    table = pd.read_csv(out / research.TABLE, keep_default_na=False)
    row = {(r["slice"], r["strategy"], r["scenario"]): r for _, r in table.iterrows()}
    undef = row[("full", "sma_trend", "atr_stop_3")]
    nan = row[("full", "buy_and_hold", "control_no_overlay")]
    zero = row[("full", "sma_trend", "control_no_overlay")]
    assert undef["sharpe"] == "" and undef["sharpe_status"] == METRIC_UNDEFINED_RETURNS
    assert nan["sharpe"] == "" and nan["sharpe_status"] == METRIC_UNDEFINED_NAN
    assert zero["sharpe"] == "0.0" and zero["sharpe_status"] == METRIC_DEFINED
    assert list(table.columns)[-3:] == ["event_levels", "event_counts", "event_warning"]
    assert set(STATUS) <= set(manifest(out)["results"]["metric_columns"])


def test_existing_csv_columns_are_unchanged(tmp_path: Path) -> None:
    rc, out = run_grid(tmp_path, suite=GridSuite())
    assert rc == 0
    table = pd.read_csv(out / research.TABLE, keep_default_na=False)
    before = [c for c in table.columns if c not in STATUS]
    assert before[:3] == ["slice", "strategy", "scenario"]
    assert before[3 : 3 + len(METRIC_KEYS)] == list(METRIC_KEYS)
    assert (table[STATUS] == METRIC_DEFINED).all().all()


def test_status_columns_come_after_columns_first_seen_in_later_rows() -> None:
    """A diagnostic key that only a later run has (e.g. a `reason_*` count) keeps the
    position it had before step 4b.1: the status columns are appended after all of them."""
    bars = make_bars([(100, 101, 99, 100)] * 6)
    report = RiskSuiteReport("1.0.0", "bt", {})
    a = run(bars, [F] * 6, CONTROL)
    b = run(bars, [F] * 6, CONTROL)
    b.diagnostics = {**b.diagnostics, "reason_only_in_b": 1.0}
    report.runs[("full", "a", CONTROL.name)] = a
    report.runs[("full", "b", CONTROL.name)] = b
    new, old = report.table(), old_table(report)
    assert list(new.columns) == list(old.columns) + STATUS
    assert list(old.columns)[-1] == "reason_only_in_b"
    pd.testing.assert_frame_equal(new[list(old.columns)], old)
