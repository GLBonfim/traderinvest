"""MLExperiment: features -> chronological split -> train (TRAIN only) -> predict ->
classification metrics + Phase 8 backtests + Phase 9 paired statistics -> edge verdict.

Research only. Re-uses the Phase 8 backtester (next-session-open execution, raw prices, Phase 8
costs, no dividends/shorting/leverage) and the Phase 9 resampling/correction primitives.
"""

import dataclasses
import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.backtest.config import BacktestConfig
from app.backtest.engine import Backtester
from app.backtest.models import BacktestResult
from app.ml.config import FEATURE_VERSION, ML_VERSION, MLConfig
from app.ml.evaluation import auc_interval, classification_metrics
from app.ml.features import FeatureDataset, build_features
from app.ml.split import PERIODS, assign_periods
from app.ml.target import HORIZON, TARGET_NAME, make_target
from app.ml.training import (
    TrainedModel,
    predict_proba,
    scoreable,
    states_from_probabilities,
    train_model,
)
from app.strategies.engine import StrategyEngine
from app.validation import comparisons as cmp
from app.validation.config import COST_SCENARIOS, CostScenario, ValidationConfig
from app.validation.metrics import matrix_metrics
from app.validation.resampling import centered_p_value, moving_block_indices, percentile_interval

FIN_KEYS = (
    "cumulative_return",
    "gross_cumulative_return",
    "cagr",
    "gross_cagr",
    "annualized_volatility",
    "sharpe",
    "gross_sharpe",
    "sortino",
    "max_drawdown",
    "max_drawdown_duration_sessions",
    "completed_trades",
    "orders",
    "win_rate",
    "exposure",
    "turnover",
    "annualized_turnover",
    "total_costs",
)
REFERENCES = (cmp.PRICE_BENCH, *cmp.TIMING)


@dataclass
class MLReport:
    experiment_id: str
    git_commit: str
    ml_version: str
    feature_version: str
    feature_fingerprint: str
    config_fingerprint: str
    config: dict[str, Any]
    target_definition: str
    sample_counts: dict[str, int] = field(default_factory=dict)
    class_distribution: list[dict[str, Any]] = field(default_factory=list)
    classification: list[dict[str, Any]] = field(default_factory=list)
    auc_intervals: list[dict[str, Any]] = field(default_factory=list)
    financial: list[dict[str, Any]] = field(default_factory=list)
    comparisons: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    model_parameters: list[dict[str, Any]] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def overall_verdict(self) -> str:
        if any(v["verdict"] == "INCREMENTAL EDGE" for v in self.verdicts):
            return "INCREMENTAL EDGE (see conditions)"
        return "NO INCREMENTAL EDGE FOUND"

    def write(self, out_dir: Path) -> Path:
        target = out_dir / self.experiment_id
        target.mkdir(parents=True, exist_ok=True)
        for name in (
            "class_distribution",
            "classification",
            "auc_intervals",
            "financial",
            "comparisons",
            "verdicts",
            "model_parameters",
        ):
            pd.DataFrame(getattr(self, name)).to_csv(target / f"{name}.csv", index=False)
        meta = {
            k: v
            for k, v in asdict(self).items()
            if k
            in (
                "experiment_id",
                "git_commit",
                "ml_version",
                "feature_version",
                "feature_fingerprint",
                "config_fingerprint",
                "config",
                "target_definition",
                "sample_counts",
                "timings_ms",
            )
        }
        meta["overall_verdict"] = self.overall_verdict
        (target / "experiment.json").write_text(json.dumps(meta, indent=2, default=str))
        return target


def _git_commit() -> str:
    """Short HEAD hash, suffixed with '+dirty' when the working tree has uncommitted changes
    (the result then does not correspond exactly to that commit)."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607 (read-only provenance)
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],  # noqa: S607 (read-only provenance)
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        return f"{head}+dirty" if dirty else head
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class MLExperiment:
    def __init__(
        self,
        config: MLConfig | None = None,
        *,
        backtest_config: BacktestConfig | None = None,
        validation_config: ValidationConfig | None = None,
        strategy_engine: StrategyEngine | None = None,
    ) -> None:
        self.config = config or MLConfig()
        self.backtest_config = backtest_config or BacktestConfig()
        self.vcfg = validation_config or ValidationConfig()
        self.strategy_engine = strategy_engine or StrategyEngine()

    # ── pipeline pieces (public for tests) ──

    def prepare(
        self, bars: pd.DataFrame, instrument_id: int | None = None
    ) -> tuple[FeatureDataset, pd.DataFrame, pd.Series]:
        dataset = build_features(bars, instrument_id=instrument_id)
        target = make_target(bars["close"].astype("float64"), dataset.frame["observed_at"])
        periods = assign_periods(pd.DatetimeIndex(dataset.frame.index), self.config.split)
        return dataset, target, periods

    def fit(
        self, dataset: FeatureDataset, target: pd.DataFrame, periods: pd.Series
    ) -> dict[str, TrainedModel]:
        return {
            m: train_model(m, self.config, dataset, target["target"], periods)
            for m in self.config.models
        }

    def windows(self) -> dict[str, tuple[date | None, date | None, str]]:
        s = self.config.split
        return {
            "test": (s.test_start, s.test_end, "out_of_sample_primary"),
            "validation": (s.validation_start, s.validation_end, "out_of_sample"),
            "late": (s.validation_start, s.test_end, "out_of_sample"),
            "early": (s.train_start, s.train_end, "in_sample_diagnostic"),
            "full": (s.train_start, s.test_end, "mixed_diagnostic"),
        }

    # ── full run ──

    def run(self, bars: pd.DataFrame, instrument_id: int | None = None) -> MLReport:
        cfg, timings = self.config, {}
        t0 = time.perf_counter()
        dataset, target, periods = self.prepare(bars, instrument_id)
        timings["features_and_target"] = _ms(t0)

        t0 = time.perf_counter()
        models = self.fit(dataset, target, periods)
        timings["preprocessing_and_training"] = _ms(t0)
        t0 = time.perf_counter()
        proba = {m: predict_proba(models[m], dataset) for m in models}
        states = {
            f"ml_{m}": states_from_probabilities(p, cfg.decision_threshold)
            for m, p in proba.items()
        }
        timings["prediction"] = _ms(t0)

        data_fp = hashlib.sha256(
            np.asarray(
                pd.util.hash_pandas_object(bars[["open", "high", "low", "close", "volume"]])
            ).tobytes()
        ).hexdigest()[:12]
        commit = _git_commit()
        report = MLReport(
            experiment_id=f"ml-{cfg.fingerprint()}-{data_fp}",
            git_commit=commit,
            ml_version=ML_VERSION,
            feature_version=FEATURE_VERSION,
            feature_fingerprint=dataset.feature_fingerprint,
            config_fingerprint=cfg.fingerprint(),
            config=asdict(cfg),
            target_definition=f"{TARGET_NAME}: 1 if raw close(T+{HORIZON}) > close(T) else 0",
        )
        usable = scoreable(dataset) & target["target"].notna()
        for period in (*PERIODS, "purged", "outside"):
            rows = usable & (periods == period)
            report.sample_counts[period] = int(rows.sum())
            if period in PERIODS:
                y = target["target"][rows]
                report.class_distribution.append(
                    {
                        "period": period,
                        "n": int(rows.sum()),
                        "up": int(y.sum()),
                        "positive_rate": float(y.mean()) if len(y) else float("nan"),
                        "first_session": str(y.index.min()) if len(y) else None,
                        "last_session": str(y.index.max()) if len(y) else None,
                    }
                )
        report.sample_counts["insufficient_features"] = int((~scoreable(dataset)).sum())
        report.sample_counts["no_target_last_row"] = int(
            (scoreable(dataset) & target["target"].isna()).sum()
        )

        for m, tm in models.items():
            report.model_parameters.append(_model_summary(m, tm, cfg))
            for period in PERIODS:
                rows = usable & (periods == period)
                y_arr = target["target"][rows].to_numpy(dtype=np.int64)
                p_arr = proba[m][rows].to_numpy(dtype=np.float64)
                row = {
                    "model": m,
                    "period": period,
                    "role": {
                        "train": "diagnostic (in-sample)",
                        "validation": "out-of-sample",
                        "test": "out-of-sample PRIMARY",
                    }[period],
                    **classification_metrics(y_arr, p_arr, cfg.decision_threshold),
                }
                report.classification.append(row)
                if period in ("validation", "test"):
                    pt, lo, hi, valid = auc_interval(
                        y_arr,
                        p_arr,
                        block_length=self.vcfg.block_length,
                        n_resamples=self.vcfg.n_resamples,
                        seed=self.vcfg.seed,
                        confidence=self.vcfg.confidence,
                    )
                    report.auc_intervals.append(
                        {
                            "model": m,
                            "period": period,
                            "roc_auc": pt,
                            "lower": lo,
                            "upper": hi,
                            "valid_resamples": valid,
                        }
                    )

        t0 = time.perf_counter()
        features_bars = bars.drop(columns=[c for c in ("adj_close",) if c in bars.columns])
        strategy_run = self.strategy_engine.run(features_bars, instrument_id=instrument_id)
        timings["baseline_strategies"] = _ms(t0)
        t0 = time.perf_counter()
        test_results: dict[str, BacktestResult] = {}
        for wname, (start, end, role) in self.windows().items():
            for scen in COST_SCENARIOS:
                results = self._backtest(bars, strategy_run, states, start, end, wname, scen)
                for sid, res in results.items():
                    report.financial.append(
                        {
                            "window": wname,
                            "role": role,
                            "scenario": scen.name,
                            "subject": sid,
                            **{k: res.metrics[k] for k in FIN_KEYS},
                        }
                    )
                if wname == "test" and scen.name == "D_default":
                    test_results = results
        timings["backtests"] = _ms(t0)

        t0 = time.perf_counter()
        report.comparisons = self._compare(test_results, list(states))
        timings["statistics"] = _ms(t0)
        report.verdicts = self._verdicts(report, list(states))
        report.timings_ms = timings
        return report

    def _backtest(
        self,
        bars: pd.DataFrame,
        run: Any,
        states: dict[str, pd.Series],
        start: date | None,
        end: date | None,
        label: str,
        scen: CostScenario,
    ) -> dict[str, BacktestResult]:
        bcfg = dataclasses.replace(
            self.backtest_config,
            start=start,
            end=end,
            period_label=label,
            slippage_bps=scen.bps,
            spread_bps=scen.spread_bps,
            commission_per_trade=scen.per_order,
            commission_bps=0.0,
            minimum_commission=0.0,
        )
        bt = Backtester(bcfg)
        out = {sid: bt.run_states(bars, s, strategy_id=sid) for sid, s in states.items()}
        return {**out, **bt.run(bars, run), **bt.benchmarks(bars)}

    def _compare(
        self, results: dict[str, BacktestResult], ml_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Paired moving-block bootstrap on TEST (Phase 9 defaults). Formal family: Sharpe
        difference of every ML model vs the price benchmark and each timing baseline."""
        v = self.vcfg
        cols = [*ml_ids, *cmp.ALL_STRATEGIES, cmp.PRICE_BENCH, cmp.TOTAL_BENCH]
        r = pd.DataFrame({c: results[c].equity["daily_return"] for c in cols}).to_numpy(
            dtype=np.float64
        )
        idx = moving_block_indices(r.shape[0], v.block_length, v.n_resamples, v.seed)
        point = matrix_metrics(r, v.periods_per_year)
        boot = {
            m: np.empty((v.n_resamples, len(cols)))
            for m in ("sharpe", "annualized_mean_return", "cagr")
        }
        for b in range(v.n_resamples):
            mb = matrix_metrics(r[idx[b]], v.periods_per_year)
            for m in boot:
                boot[m][b] = mb[m]
        pos = {c: j for j, c in enumerate(cols)}
        rows: list[dict[str, Any]] = []
        for a in ml_ids:
            refs = [(ref, "formal", "sharpe") for ref in REFERENCES]
            refs += [
                (ref, "descriptive", m)
                for ref in (*REFERENCES, "buy_and_hold", cmp.TOTAL_BENCH)
                for m in ("annualized_mean_return", "cagr")
            ]
            refs += [
                (cmp.TOTAL_BENCH, "descriptive", "sharpe"),
                ("buy_and_hold", "descriptive", "sharpe"),
            ]
            for ref, family, m in refs:
                d = boot[m][:, pos[a]] - boot[m][:, pos[ref]]
                est = float(point[m][pos[a]] - point[m][pos[ref]])
                lo, hi, _ = percentile_interval(d, v.confidence)
                rows.append(
                    {
                        "a": a,
                        "b": ref,
                        "metric": m,
                        "family": family,
                        "estimate": est,
                        "lower": lo,
                        "upper": hi,
                        "p_value": centered_p_value(d, est) if family == "formal" else float("nan"),
                        "window": "test",
                        "scenario": "D_default",
                        "block_length": v.block_length,
                        "n_resamples": v.n_resamples,
                        "seed": v.seed,
                        "confidence": v.confidence,
                    }
                )
        formal = [i for i, row in enumerate(rows) if row["family"] == "formal"]
        p = np.array([rows[i]["p_value"] for i in formal])
        holm, bh = cmp.holm(p, m=len(formal)), cmp.benjamini_hochberg(p, m=len(formal))
        for k, i in enumerate(formal):
            rows[i].update(p_holm=float(holm[k]), p_bh=float(bh[k]), family_size=len(formal))
        return rows

    def _verdicts(self, report: MLReport, ml_ids: list[str]) -> list[dict[str, Any]]:
        rule = self.config.edge_rule
        fin = pd.DataFrame(report.financial)
        stress = fin[
            (fin["window"] == "test") & (fin["scenario"] == rule.stress_scenario)
        ].set_index("subject")
        out = []
        for sid in ml_ids:
            model = sid.removeprefix("ml_")
            auc = next(
                a for a in report.auc_intervals if a["model"] == model and a["period"] == "test"
            )
            formal = [c for c in report.comparisons if c["a"] == sid and c["family"] == "formal"]
            auc_ok = bool(np.isfinite(auc["lower"]) and auc["lower"] > rule.auc_floor)
            sharpe_ok = all(c["estimate"] > 0 and c["p_holm"] < rule.alpha for c in formal)
            stress_sharpe = stress["sharpe"].astype("float64")
            cost_ok = bool(stress_sharpe[sid] > stress_sharpe[cmp.PRICE_BENCH])
            out.append(
                {
                    "model": model,
                    "auc_lower_above_floor": auc_ok,
                    "sharpe_beats_all_references_after_holm": sharpe_ok,
                    "sharpe_beats_benchmark_at_10bps": cost_ok,
                    "verdict": "INCREMENTAL EDGE"
                    if auc_ok and sharpe_ok and cost_ok
                    else "NO INCREMENTAL EDGE FOUND",
                }
            )
        return out


def _model_summary(name: str, tm: TrainedModel, cfg: MLConfig) -> dict[str, Any]:
    est = tm.estimator
    info: dict[str, Any] = {
        "model": name,
        "seed": tm.seed,
        "train_rows": len(tm.train_index),
        "train_first": str(tm.train_index.min()),
        "train_last": str(tm.train_index.max()),
        "inputs": len(tm.preprocessor.output_columns),
    }
    if hasattr(est, "coef_"):
        coefs = pd.Series(est.coef_[0], index=tm.preprocessor.output_columns)
        top = coefs.abs().sort_values(ascending=False).head(10)
        info["top_abs_coefficients"] = {k: round(float(coefs[k]), 6) for k in top.index}
        info["intercept"] = float(est.intercept_[0])
    if hasattr(est, "feature_importances_"):
        imp = pd.Series(est.feature_importances_, index=tm.preprocessor.output_columns)
        info["top_importances"] = {
            k: round(float(v), 6) for k, v in imp.sort_values(ascending=False).head(10).items()
        }
    return info


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)
