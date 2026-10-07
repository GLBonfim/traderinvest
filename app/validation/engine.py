"""Validator: Phase 8 backtests -> uncertainty intervals, comparisons, cost sensitivity.

Point-in-time: each slice's strategies are computed on bars up to the slice end only; each
backtest window is [slice start, slice end]; resampling draws only positions inside the slice.
Inputs are never modified. Results are exploratory/descriptive; see docs/statistical-validation.md.
"""

import dataclasses
import time
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.backtest.config import BacktestConfig
from app.backtest.engine import Backtester
from app.backtest.models import BacktestResult
from app.strategies.engine import StrategyEngine, StrategyRun
from app.validation import comparisons as cmp
from app.validation.config import VALIDATION_VERSION, CostScenario, SliceSpec, ValidationConfig
from app.validation.metrics import METRICS, concentration, matrix_metrics
from app.validation.models import (
    Comparison,
    ConcentrationRow,
    CostSensitivityRow,
    MetricInterval,
    Provenance,
    RedundancyRow,
    ValidationReport,
)
from app.validation.resampling import centered_p_value, moving_block_indices, percentile_interval

RETURN_TOLERANCE = 1e-12
NOTES = {
    "max_drawdown": "path-dependent: block resampling preserves dependence only within blocks; "
    "interval is approximate",
    "sortino": "undefined in resamples without negative returns (excluded)",
}


class Validator:
    def __init__(
        self,
        config: ValidationConfig | None = None,
        *,
        backtest_config: BacktestConfig | None = None,
        strategy_engine: StrategyEngine | None = None,
    ) -> None:
        self.config = config or ValidationConfig()
        self.backtest_config = backtest_config or BacktestConfig()
        self.strategy_engine = strategy_engine or StrategyEngine()

    # ── backtests per slice / scenario ──

    def _strategy_run(
        self, bars: pd.DataFrame, end: date | None
    ) -> tuple[pd.DataFrame, StrategyRun]:
        upto = bars[pd.DatetimeIndex(bars.index).date <= end] if end else bars
        if upto.empty:
            raise ValueError("no bars on or before slice end")
        features = upto.drop(columns=[c for c in ("adj_close",) if c in upto.columns])
        return upto, self.strategy_engine.run(features)

    def _backtest(
        self, upto: pd.DataFrame, run: StrategyRun, sl: SliceSpec, scenario: CostScenario
    ) -> tuple[dict[str, BacktestResult], BacktestConfig]:
        cfg = dataclasses.replace(
            self.backtest_config,
            start=sl.start,
            end=sl.end,
            period_label=sl.name,
            slippage_bps=scenario.bps,
            spread_bps=scenario.spread_bps,
            commission_per_trade=scenario.per_order,
            commission_bps=0.0,
            minimum_commission=0.0,
        )
        bt = Backtester(cfg)
        return {**bt.run(upto, run), **bt.benchmarks(upto)}, cfg

    # ── statistics on one slice ──

    def analyze_returns(
        self,
        returns: pd.DataFrame,
        *,
        slice_name: str,
        cost_scenario: str,
        backtest_fingerprint: str,
    ) -> tuple[list[MetricInterval], list[Comparison]]:
        """Bootstrap every column of `returns` (rows = sessions of ONE slice) with paired
        moving-block resampling; returns intervals and (unadjusted) comparisons."""
        cfg = self.config
        if returns.isna().any().any():
            raise ValueError("returns contain missing values")
        r = returns.to_numpy(dtype=np.float64)
        n, cols = r.shape[0], list(returns.columns)
        prov = Provenance(
            VALIDATION_VERSION, cfg.fingerprint(), cfg.method, cfg.block_length, cfg.n_resamples,
            cfg.confidence, cfg.seed, slice_name, returns.index[0], returns.index[-1], n,
            cost_scenario, backtest_fingerprint,
        )  # fmt: skip
        point = matrix_metrics(r, cfg.periods_per_year)
        idx = moving_block_indices(n, cfg.block_length, cfg.n_resamples, cfg.seed)
        boot = {m: np.empty((cfg.n_resamples, len(cols))) for m in METRICS}
        for rs in range(cfg.n_resamples):
            mb = matrix_metrics(r[idx[rs]], cfg.periods_per_year)
            for m in METRICS:
                boot[m][rs] = mb[m]

        intervals = []
        for j, col in enumerate(cols):
            for m in METRICS:
                lo, hi, valid = percentile_interval(boot[m][:, j], cfg.confidence)
                intervals.append(
                    MetricInterval(
                        col, m, float(point[m][j]), lo, hi, valid, NOTES.get(m, ""), prov
                    )
                )

        pos = {c: j for j, c in enumerate(cols)}
        comps: list[Comparison] = []

        def add(a: str, b: str, metric: str, family: str) -> None:
            if a not in pos or b not in pos:
                return
            diff_boot = boot[metric][:, pos[a]] - boot[metric][:, pos[b]]
            est = float(point[metric][pos[a]] - point[metric][pos[b]])
            lo, hi, _ = percentile_interval(diff_boot, cfg.confidence)
            p = centered_p_value(diff_boot, est) if family == "formal" else float("nan")
            null = (
                f"paired difference in {metric} = 0" if family == "formal" else "none (descriptive)"
            )
            comps.append(
                Comparison(
                    comparison_id=f"{slice_name}:{a}-vs-{b}:{metric}",
                    family=family, a=a, b=b, metric=metric,
                    null_hypothesis=null,
                    estimate=est, lower=lo, upper=hi, p_value=p, p_holm=float("nan"),
                    p_bh=float("nan"), family_size=0, note="", provenance=prov,
                )
            )  # fmt: skip

        formal = slice_name == cfg.formal_slice and cost_scenario == cfg.default_scenario
        for a, b in cmp.formal_pairs():
            for m in cmp.FORMAL_METRICS:
                add(a, b, m, "formal" if formal else "descriptive")
        for a, b in cmp.descriptive_pairs():
            for m in cmp.DESCRIPTIVE_METRICS:
                add(a, b, m, "descriptive")
        return intervals, comps

    # ── full run ──

    def run(self, bars: pd.DataFrame) -> ValidationReport:
        cfg = self.config
        report = ValidationReport(VALIDATION_VERSION, cfg.fingerprint(), dataclasses.asdict(cfg))
        runs: dict[date | None, tuple[pd.DataFrame, StrategyRun]] = {}
        default = next(c for c in cfg.cost_scenarios if c.name == cfg.default_scenario)

        for sl in cfg.slices:
            t0 = time.perf_counter()
            if sl.end not in runs:
                runs[sl.end] = self._strategy_run(bars, sl.end)
            upto, run = runs[sl.end]
            report.timings_ms[f"{sl.name}:strategies"] = round((time.perf_counter() - t0) * 1000, 1)

            t0 = time.perf_counter()
            by_scenario: dict[str, tuple[dict[str, BacktestResult], BacktestConfig]] = {}
            for scen in cfg.cost_scenarios:
                by_scenario[scen.name] = self._backtest(upto, run, sl, scen)
                results, bcfg = by_scenario[scen.name]
                for sid, res in results.items():
                    m = res.metrics
                    report.cost_sensitivity.append(
                        CostSensitivityRow(
                            sl.name, scen.name, sid, m["cumulative_return"], m["cagr"], m["sharpe"],
                            m["max_drawdown"], m["completed_trades"], m["orders"],
                            m["annualized_turnover"], m["total_costs"], bcfg.fingerprint(),
                        )
                    )  # fmt: skip
            report.timings_ms[f"{sl.name}:backtests"] = round((time.perf_counter() - t0) * 1000, 1)

            results, bcfg = by_scenario[default.name]
            returns = pd.DataFrame(
                {sid: res.equity["daily_return"] for sid, res in results.items()}
            )
            for sid in cmp.ALL_STRATEGIES:
                returns[f"{sid}__gross"] = results[sid].equity["gross_daily_return"]

            t0 = time.perf_counter()
            intervals, comps = self.analyze_returns(
                returns,
                slice_name=sl.name,
                cost_scenario=default.name,
                backtest_fingerprint=bcfg.fingerprint(),
            )
            report.intervals += intervals
            report.comparisons += comps
            report.timings_ms[f"{sl.name}:bootstrap"] = round((time.perf_counter() - t0) * 1000, 1)

            report.redundancy.append(
                _redundancy(sl.name, results, "price_action_trend", "regime_trend")
            )
            for sid in (*cmp.ALL_STRATEGIES, cmp.PRICE_BENCH, cmp.TOTAL_BENCH):
                c = concentration(results[sid].equity["daily_return"].to_numpy(dtype=np.float64))
                report.concentration.append(
                    ConcentrationRow(
                        sl.name, sid, c["cumulative_return"],
                        c["cumulative_return_without_best_10_days"],
                        c["log_return_share_of_top_1pct_days"],
                        float(results[sid].metrics["top5_winners_share_of_gross_profit"]),
                    )
                )  # fmt: skip

        report.comparisons = _adjust_formal_family(report.comparisons)
        return report


def _adjust_formal_family(comparisons: list[Comparison]) -> list[Comparison]:
    formal = [i for i, c in enumerate(comparisons) if c.family == "formal"]
    p = np.array([comparisons[i].p_value for i in formal], dtype=np.float64)
    holm, bh = cmp.holm(p, m=len(formal)), cmp.benjamini_hochberg(p, m=len(formal))
    out = list(comparisons)
    for k, i in enumerate(formal):
        note = (
            "formal test; interpret adjusted p-values only; not evidence of economic value"
            if np.isfinite(p[k])
            else "formal test UNDEFINED (e.g. zero-variance series); counted in the family size"
        )
        out[i] = dataclasses.replace(
            comparisons[i], p_holm=float(holm[k]), p_bh=float(bh[k]), family_size=len(formal),
            note=note,
        )  # fmt: skip
    return out


def _redundancy(
    slice_name: str, results: dict[str, BacktestResult], a: str, b: str
) -> RedundancyRow:
    ea, eb = results[a].equity, results[b].equity
    ra, rb = ea["daily_return"], eb["daily_return"]
    return RedundancyRow(
        slice_name=slice_name,
        a=a,
        b=b,
        position_agreement=float((ea["position"] == eb["position"]).mean()),
        daily_return_correlation=_correlation(ra.to_numpy(), rb.to_numpy()),
        # tolerance: fully-invested accounting leaves ~1e-11 cash residues, so returns of
        # identical positions can differ at ~1e-16; only differences > 1e-12 are counted
        sessions_with_different_returns=int(((ra - rb).abs() > RETURN_TOLERANCE).sum()),
        sessions=len(ra),
    )


def _correlation(x: np.ndarray, y: np.ndarray) -> float:
    """Pearson correlation; NaN (undefined) when either series is constant."""
    if len(x) < 2 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def report_summary(report: ValidationReport) -> dict[str, Any]:
    f = report.frames()
    comps = f["comparisons"]
    formal = comps[comps["family"] == "formal"]
    return {
        "validation_version": report.validation_version,
        "config_fingerprint": report.config_fingerprint,
        "formal_family_size": len(formal),
        "formal_holm_below_0_05": int((formal["p_holm"] < 0.05).sum()),
        "formal_bh_below_0_05": int((formal["p_bh"] < 0.05).sum()),
        "intervals": len(f["intervals"]),
        "comparisons": len(comps),
        "timings_ms": report.timings_ms,
    }
