"""Risk suite: every baseline x every pre-declared scenario x every fixed slice, plus
DESCRIPTIVE paired comparisons of each overlay against the no-overlay control.

No scenario is selected or ranked. Comparisons are intervals only (no p-values): the scenarios
are infrastructure checks, not hypotheses about value; their count is reported so that any
later formal use can account for multiplicity.
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
from app.data.calendar import TradingCalendar
from app.indicators.engine import IndicatorEngine
from app.risk.config import RISK_VERSION, SCENARIOS, RiskConfig
from app.risk.engine import RiskOverlay, RiskRun
from app.strategies.engine import StrategyEngine
from app.validation.config import DEFAULT_SLICES, SliceSpec, ValidationConfig
from app.validation.metrics import matrix_metrics
from app.validation.resampling import moving_block_indices, percentile_interval

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
        return pd.DataFrame(rows)


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
        cols = {sc: report.runs[(slice_name, sid, sc)].equity["daily_return"] for sc in scenarios}
        r = pd.DataFrame(cols).to_numpy(np.float64)
        idx = moving_block_indices(r.shape[0], v.block_length, v.n_resamples, v.seed)
        point = matrix_metrics(r, v.periods_per_year)
        keys = ("sharpe", "annualized_volatility", "max_drawdown", "cagr")
        boot = {k: np.empty((v.n_resamples, len(scenarios))) for k in keys}
        for b in range(v.n_resamples):
            m = matrix_metrics(r[idx[b]], v.periods_per_year)
            for k in keys:
                boot[k][b] = m[k]
        for j, sc in enumerate(scenarios[1:], start=1):
            for k in keys:
                lo, hi, _ = percentile_interval(boot[k][:, j] - boot[k][:, 0], v.confidence)
                out.append(
                    {
                        "slice": slice_name,
                        "strategy": sid,
                        "scenario": sc,
                        "vs": scenarios[0],
                        "metric": k,
                        "estimate": float(point[k][j] - point[k][0]),
                        "lower": lo,
                        "upper": hi,
                        "family": "descriptive",
                        "block_length": v.block_length,
                        "n_resamples": v.n_resamples,
                        "seed": v.seed,
                    }
                )
    return out
