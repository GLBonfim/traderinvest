"""Risk suite: every baseline x every pre-declared scenario x every fixed slice, plus
DESCRIPTIVE paired comparisons of each overlay against the no-overlay control.

No scenario is selected or ranked. Comparisons are intervals only (no p-values): the scenarios
are infrastructure checks, not hypotheses about value; their count is reported so that any
later formal use can account for multiplicity.

The intervals are EXPLORATORY percentile intervals of a moving-block bootstrap of the REALIZED
daily returns, not validated confidence intervals: resampling an overlay's realized returns does
not re-run the overlay on resampled inputs (a path-dependent control such as a drawdown lock or
a stop is not re-applied), and for order-dependent metrics (max drawdown) block resampling also
breaks the drawdown paths themselves. Each row carries the bootstrap median, the number of
finite samples, `order_dependent`, `interval_kind` and whether the point estimate falls inside
its own interval, so that a biased distribution is visible.
"""

import dataclasses
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.backtest.config import BacktestConfig
from app.backtest.engine import session_times
from app.backtest.metrics import return_metric_status, undefined_mask
from app.data.calendar import TradingCalendar
from app.indicators.engine import IndicatorEngine
from app.risk.config import RISK_VERSION, SCENARIOS, RiskConfig
from app.risk.engine import RiskOverlay, RiskRun
from app.strategies.engine import StrategyEngine
from app.validation.config import DEFAULT_SLICES, SliceSpec, ValidationConfig
from app.validation.metrics import matrix_metrics
from app.validation.resampling import bootstrap_summary, moving_block_indices

METRIC_KEYS = (
    "cumulative_return",
    "gross_cumulative_return",
    "cagr",
    "annualized_volatility",
    "sharpe",
    "sortino",
    "max_drawdown",
    "max_drawdown_duration_sessions",
    "completed_trades",
    "orders",
    "exposure",
    "turnover",
    "annualized_turnover",
    "total_costs",
    "realized_pnl",
    "unrealized_pnl",
)
COMPARED_METRICS = ("sharpe", "annualized_volatility", "max_drawdown", "cagr")
# Metrics whose value depends on the ORDER of the returns (not only on their multiset).
ORDER_DEPENDENT_METRICS = frozenset({"max_drawdown"})
INTERVAL_KIND = "exploratory_block_bootstrap_percentile"
ORDER_DEPENDENT_NOTE = (
    "path-dependent metric: block resampling of realized returns breaks drawdown paths and "
    "does not re-run the policy; exploratory only, not a confidence interval"
)


@dataclass
class RiskSuiteReport:
    risk_version: str
    backtest_fingerprint: str
    scenarios: dict[str, str]  # name -> risk fingerprint
    runs: dict[tuple[str, str, str], RiskRun] = field(
        default_factory=dict
    )  # (slice, strategy, scenario)
    comparisons: list[dict[str, Any]] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)

    def table(self) -> pd.DataFrame:
        rows = [
            {
                "slice": s,
                "strategy": st,
                "scenario": sc,
                **{k: r.metrics[k] for k in METRIC_KEYS},
                **r.diagnostics,
            }
            for (s, st, sc), r in self.runs.items()
        ]
        table = pd.DataFrame(rows)
        # appended after every existing column: why a return-based metric is missing
        # (ADR-0034 D-A5, step 4b.1); set from the metrics, never from the cell
        status = [return_metric_status(r.metrics) for r in self.runs.values()]
        for col in status[0] if status else ():
            table[col] = [st[col] for st in status]
        return table


def run_risk_suite(
    bars: pd.DataFrame,
    *,
    scenarios: tuple[RiskConfig, ...] = SCENARIOS,
    slices: tuple[SliceSpec, ...] = DEFAULT_SLICES,
    backtest: BacktestConfig | None = None,
    strategy_engine: StrategyEngine | None = None,
    validation: ValidationConfig | None = None,
    compare: bool = True,
) -> RiskSuiteReport:
    bt = backtest or BacktestConfig()
    engine = strategy_engine or StrategyEngine()
    vcfg = validation or ValidationConfig()
    if scenarios[0].name != "control_no_overlay":
        raise ValueError("the no-overlay control must be the first scenario")
    report = RiskSuiteReport(
        RISK_VERSION, bt.fingerprint(), {s.name: s.fingerprint() for s in scenarios}
    )
    calendar = TradingCalendar("XNYS")
    for sl in slices:
        t0 = time.perf_counter()
        upto = (
            bars[pd.DatetimeIndex(bars.index).date <= sl.end] if sl.end else bars
        )  # nothing after end
        ohlcv = upto[["open", "high", "low", "close", "volume"]]
        run = engine.run(ohlcv)
        indicators = IndicatorEngine().analyze(ohlcv).values
        times = session_times(pd.DatetimeIndex(upto.index), calendar)
        start = _start_index(upto, sl.start)
        report.timings_ms[f"{sl.name}:inputs"] = round((time.perf_counter() - t0) * 1000, 1)
        t0 = time.perf_counter()
        for sid, g in run.signals.groupby("strategy_id", sort=False):
            states = pd.Series(g["state"].to_numpy(), index=pd.DatetimeIndex(g["bar_ts"]))
            for risk in scenarios:
                cfg = dataclasses.replace(bt, start=sl.start, end=sl.end, period_label=sl.name)
                report.runs[(sl.name, str(sid), risk.name)] = RiskOverlay(risk, cfg).run(
                    upto, states, indicators, times, strategy_id=str(sid), start_index=start
                )
        report.timings_ms[f"{sl.name}:overlays"] = round((time.perf_counter() - t0) * 1000, 1)
        if compare:
            t0 = time.perf_counter()
            report.comparisons += _compare(report, sl.name, [s.name for s in scenarios], vcfg)
            report.timings_ms[f"{sl.name}:comparisons"] = round(
                (time.perf_counter() - t0) * 1000, 1
            )
    return report


def _start_index(bars: pd.DataFrame, start: date | None) -> int:
    if start is None:
        return 0
    pos = np.flatnonzero(pd.DatetimeIndex(bars.index).date >= start)
    if not pos.size:
        raise ValueError("no bars on or after slice start")
    return int(pos[0])


def _compare(
    report: RiskSuiteReport, slice_name: str, scenarios: list[str], v: ValidationConfig
) -> list[dict[str, Any]]:
    """Paired moving-block bootstrap (Phase 9 primitives) of overlay - control, per strategy."""
    out: list[dict[str, Any]] = []
    strategies = sorted({st for (s, st, _) in report.runs if s == slice_name})
    for sid in strategies:
        series = {sc: report.runs[(slice_name, sid, sc)].equity["daily_return"] for sc in scenarios}
        # D-A5: a series with an undefined return is never used as a complete series (no
        # session dropped, no value imputed); its comparisons are reported as not testable.
        undefined = [sc for sc in scenarios if undefined_mask(series[sc]).any()]
        usable = (
            [sc for sc in scenarios if sc not in undefined] if scenarios[0] not in undefined else []
        )
        keys = COMPARED_METRICS
        if usable:
            cols = {sc: series[sc].to_numpy(np.float64, na_value=np.nan) for sc in usable}
            r = pd.DataFrame(cols).to_numpy(np.float64)
            idx = moving_block_indices(r.shape[0], v.block_length, v.n_resamples, v.seed)
            point = matrix_metrics(r, v.periods_per_year)
            boot = {k: np.empty((v.n_resamples, len(usable))) for k in keys}
            for b in range(v.n_resamples):
                m = matrix_metrics(r[idx[b]], v.periods_per_year)
                for k in keys:
                    boot[k][b] = m[k]
        for sc in scenarios[1:]:
            if sc not in usable:
                which = [x for x in (scenarios[0], sc) if x in undefined]
                out += [_not_testable(slice_name, sid, sc, scenarios[0], k, which, v) for k in keys]
                continue
            j = usable.index(sc)
            for k in keys:
                bs = bootstrap_summary(boot[k][:, j] - boot[k][:, 0], v.confidence)
                estimate = float(point[k][j] - point[k][0])
                inside = (
                    bool(bs.lower <= estimate <= bs.upper)
                    if np.isfinite([estimate, bs.lower, bs.upper]).all()
                    else None
                )
                order_dependent = k in ORDER_DEPENDENT_METRICS
                out.append(
                    {
                        "slice": slice_name,
                        "strategy": sid,
                        "scenario": sc,
                        "vs": scenarios[0],
                        "metric": k,
                        "estimate": estimate,
                        "lower": bs.lower,
                        "upper": bs.upper,
                        "family": "descriptive",
                        "block_length": v.block_length,
                        "n_resamples": v.n_resamples,
                        "seed": v.seed,
                        # appended columns (existing ones keep their names and order)
                        "bootstrap_median": bs.median,
                        "n_finite": bs.n_finite,
                        "order_dependent": order_dependent,
                        "interval_kind": INTERVAL_KIND,
                        "estimate_in_interval": inside,
                        "note": ORDER_DEPENDENT_NOTE if order_dependent else "",
                    }
                )
    return out


NOT_TESTABLE_NOTE = (
    "not testable: undefined daily return(s) in {which} (previous equity 0); no session "
    "dropped, no value imputed"
)


def _not_testable(
    slice_name: str, sid: str, sc: str, vs: str, metric: str, which: list[str], v: ValidationConfig
) -> dict[str, Any]:
    """A comparison row that is not computed (same columns as a computed row)."""
    nan = float("nan")
    return {
        "slice": slice_name,
        "strategy": sid,
        "scenario": sc,
        "vs": vs,
        "metric": metric,
        "estimate": nan,
        "lower": nan,
        "upper": nan,
        "family": "descriptive",
        "block_length": v.block_length,
        "n_resamples": v.n_resamples,
        "seed": v.seed,
        "bootstrap_median": nan,
        "n_finite": 0,
        "order_dependent": metric in ORDER_DEPENDENT_METRICS,
        "interval_kind": INTERVAL_KIND,
        "estimate_in_interval": None,
        "note": NOT_TESTABLE_NOTE.format(which=" and ".join(which)),
    }
