"""Research results: Phase 8 backtests, Phase 9 validation, Phase 10 ML, Phase 11 risk.

Phase 8 backtests and single risk overlays are cheap and deterministic: computed on demand and
cached by the UI (dataset identity + configuration). Phase 9 validation (~1 min) and Phase 10
ML (~2 min, trains models) are NEVER run because a page was opened: they are read from results
persisted under the git-ignored `data/dashboard/research/`, keyed by dataset identity and
configuration fingerprint, and recomputed only by an explicit user action.
"""

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app import __version__
from app.backtest.config import BacktestConfig
from app.backtest.engine import BENCHMARK_PRICE, BENCHMARK_TOTAL, run_suite, session_times
from app.backtest.models import BacktestResult
from app.dashboard.services.analysis import EngineBundle, strategy_input_series
from app.data.calendar import TradingCalendar
from app.ml.config import MLConfig
from app.ml.engine import MLExperiment
from app.ml.target import TARGET_NAME
from app.ml.training import predict_proba
from app.risk.config import SCENARIOS, RiskConfig
from app.risk.engine import RiskOverlay, RiskRun
from app.validation import comparisons as cmp
from app.validation.config import ValidationConfig
from app.validation.engine import Validator

BENCHMARK_NOTES = {
    BENCHMARK_PRICE: "SPY price-return buy & hold: valued with raw prices (no dividends). "
    "The like-for-like reference for the timing strategies, which also earn price return only.",
    BENCHMARK_TOTAL: "SPY total-return buy & hold: valued with the provider's adjusted close "
    "(dividends reinvested). Not like-for-like with the price-only timing strategies.",
}
BACKTEST_COLUMNS = (
    "cumulative_return",
    "gross_cumulative_return",
    "cost_drag_cumulative",
    "cagr",
    "annualized_volatility",
    "sharpe",
    "sortino",
    "max_drawdown",
    "max_drawdown_duration_sessions",
    "completed_trades",
    "win_rate",
    "avg_trade_net_return",
    "median_trade_net_return",
    "exposure",
    "annualized_turnover",
    "total_costs",
    "avg_holding_sessions",
)
ALPHA = 0.05


class ResultsMissingError(LookupError):
    """No persisted result for this dataset and configuration (an explicit run is needed)."""


# ── Phase 8 ──


def backtest_suite(bars: pd.DataFrame, config: BacktestConfig | None = None) -> dict[str, Any]:
    results = run_suite(bars, config=config or BacktestConfig())
    table = pd.DataFrame({sid: r.metrics for sid, r in results.items()}).T
    return {"results": results, "table": table[list(BACKTEST_COLUMNS)]}


def equity_curves(results: dict[str, BacktestResult], gross: bool = False) -> pd.DataFrame:
    col = "gross_cumulative_return" if gross else "cumulative_return"
    return pd.DataFrame({sid: r.equity[col] for sid, r in results.items()})


def drawdown_curves(results: dict[str, BacktestResult]) -> pd.DataFrame:
    return pd.DataFrame({sid: r.equity["drawdown"] for sid, r in results.items()})


# ── persistence helpers ──


def _write_frames(target: Path, frames: dict[str, pd.DataFrame], meta: dict[str, Any]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        frame.to_csv(target / f"{name}.csv", index=False)
    (target / "meta.json").write_text(json.dumps(meta, indent=2, default=str))


def _read_frames(target: Path, names: tuple[str, ...]) -> tuple[dict[str, pd.DataFrame], dict]:  # type: ignore[type-arg]
    if not (target / "meta.json").exists():
        raise ResultsMissingError(str(target))
    meta = json.loads((target / "meta.json").read_text())
    return {n: pd.read_csv(target / f"{n}.csv") for n in names}, meta


# ── Phase 9 ──

VALIDATION_FRAMES = ("intervals", "comparisons", "cost_sensitivity", "redundancy", "concentration")


def validation_dir(root: Path, dataset_key: str, config: ValidationConfig) -> Path:
    return root / "validation" / f"{dataset_key}-{config.fingerprint()}"


def compute_validation(
    bars: pd.DataFrame, root: Path, dataset_key: str, config: ValidationConfig | None = None
) -> Path:
    """Explicit action: runs the Phase 9 Validator unchanged and persists its tables."""
    cfg = config or ValidationConfig()
    report = Validator(cfg).run(bars)
    target = validation_dir(root, dataset_key, cfg)
    meta = {
        "validation_version": report.validation_version,
        "config_fingerprint": report.config_fingerprint,
        "dataset_key": dataset_key,
        "app_version": __version__,
        "created_at": datetime.now(UTC).isoformat(),
        "timings_ms": report.timings_ms,
        "config": report.config,
    }
    _write_frames(target, report.frames(), meta)
    return target


def load_validation(
    root: Path, dataset_key: str, config: ValidationConfig | None = None
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    return _read_frames(
        validation_dir(root, dataset_key, config or ValidationConfig()), VALIDATION_FRAMES
    )


def classify_formal(comparisons: pd.DataFrame, alpha: float = ALPHA) -> pd.DataFrame:
    """Exact status of each pre-declared formal test from its Holm-adjusted p-value."""
    f = comparisons[comparisons["family"] == "formal"].copy()

    def status(p: float) -> str:
        if not np.isfinite(p):
            return "undefined (insufficient evidence)"
        return f"significant at {alpha:g} (Holm)" if p < alpha else f"not significant at {alpha:g}"

    f["status"] = f["p_holm"].map(status)
    return f


def validation_conclusion(comparisons: pd.DataFrame, alpha: float = ALPHA) -> str:
    """Project conclusion rule (Phase 9): an edge would require a timing strategy with a
    positive Holm-significant difference vs the price benchmark."""
    f = comparisons[comparisons["family"] == "formal"]
    vs_bench = f[(f["b"] == cmp.PRICE_BENCH) & f["a"].isin(cmp.TIMING)]
    edge = vs_bench[(vs_bench["estimate"] > 0) & (vs_bench["p_holm"] < alpha)]
    if edge.empty:
        return "NO STATISTICAL EDGE FOUND"
    return "Holm-significant positive difference(s) vs the price benchmark: " + ", ".join(
        f"{r.a} ({r.metric})" for r in edge.itertuples()
    )


# ── Phase 10 ──

ML_FRAMES = (
    "classification",
    "auc_intervals",
    "financial",
    "comparisons",
    "verdicts",
    "class_distribution",
    "model_parameters",
)


def ml_dir(root: Path, experiment_id: str) -> Path:
    return root / "ml" / experiment_id


def ml_experiment_id(bars: pd.DataFrame, config: MLConfig | None = None) -> str:
    """Same identity as `MLExperiment.run` (config fingerprint + OHLCV data fingerprint)."""
    import hashlib

    cfg = config or MLConfig()
    data_fp = hashlib.sha256(
        np.asarray(
            pd.util.hash_pandas_object(bars[["open", "high", "low", "close", "volume"]])
        ).tobytes()
    ).hexdigest()[:12]
    return f"ml-{cfg.fingerprint()}-{data_fp}"


def compute_ml(bars: pd.DataFrame, root: Path, instrument_id: int | None = None) -> Path:
    """Explicit action (trains the two pre-declared models on TRAIN only): runs the Phase 10
    experiment unchanged, writes its report, and stores the per-session probabilities of the
    same deterministic models for the distribution/calibration views."""
    exp = MLExperiment()
    report = exp.run(bars, instrument_id=instrument_id)
    target = report.write(root / "ml")
    dataset, target_df, periods = exp.prepare(bars, instrument_id)
    models = exp.fit(dataset, target_df, periods)
    preds = pd.DataFrame(
        {
            "bar_ts": dataset.frame.index,
            "period": periods.to_numpy(),
            "target": target_df["target"].to_numpy(),
            **{f"p_{m}": predict_proba(tm, dataset).to_numpy() for m, tm in models.items()},
        }
    )
    preds.to_csv(target / "predictions.csv", index=False)
    meta = json.loads((target / "experiment.json").read_text())
    meta["feature_count"] = len(dataset.feature_names)
    meta["app_version"] = __version__
    (target / "experiment.json").write_text(json.dumps(meta, indent=2, default=str))
    return target


def load_ml(root: Path, experiment_id: str) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    target = ml_dir(root, experiment_id)
    if not (target / "experiment.json").exists():
        raise ResultsMissingError(str(target))
    meta = json.loads((target / "experiment.json").read_text())
    frames = {n: pd.read_csv(target / f"{n}.csv") for n in ML_FRAMES}
    preds = target / "predictions.csv"
    if preds.exists():
        frames["predictions"] = pd.read_csv(preds)
    return frames, meta


def calibration_table(
    preds: pd.DataFrame, model: str, period: str = "test", bins: int = 10
) -> pd.DataFrame:
    """Reliability table: mean predicted P(up) vs observed up-rate per probability decile."""
    d = preds[(preds["period"] == period) & preds["target"].notna()].dropna(subset=[f"p_{model}"])
    if d.empty:
        return pd.DataFrame(columns=["bin", "n", "mean_predicted", "observed_rate"])
    q = pd.qcut(d[f"p_{model}"], q=bins, duplicates="drop")
    g = d.groupby(q, observed=True)
    return pd.DataFrame(
        {
            "bin": [str(i) for i in g.groups],
            "n": g.size().to_numpy(),
            "mean_predicted": g[f"p_{model}"].mean().to_numpy(),
            "observed_rate": g["target"].mean().to_numpy(),
        }
    )


def ml_summary(frames: dict[str, pd.DataFrame], meta: dict[str, Any]) -> dict[str, Any]:
    split = meta["config"]["split"]
    return {
        "experiment_id": meta["experiment_id"],
        "target": meta.get("target_definition", TARGET_NAME),
        "feature_count": meta.get("feature_count"),
        "train": f"{split['train_start'] or 'first valid row'} -> {split['train_end']}",
        "validation": f"{split['validation_start']} -> {split['validation_end']}",
        "test": f"{split['test_start']} -> {split['test_end'] or 'last session'}",
        "overall_verdict": meta.get("overall_verdict"),
        "git_commit": meta.get("git_commit"),
        "models": sorted(frames["verdicts"]["model"].tolist()),
    }


# ── Phase 11 ──


def scenario(name: str) -> RiskConfig:
    for s in SCENARIOS:
        if s.name == name:
            return s
    raise KeyError(f"unknown risk scenario {name!r}")


def risk_run(bars: pd.DataFrame, b: EngineBundle, strategy_id: str, scenario_name: str) -> RiskRun:
    times = session_times(pd.DatetimeIndex(bars.index), TradingCalendar("XNYS"))
    return RiskOverlay(scenario(scenario_name)).run(
        bars,
        strategy_input_series(b, strategy_id),
        b.indicators.values,
        times,
        strategy_id=strategy_id,
    )


def risk_decision_at(run: RiskRun, ts: pd.Timestamp) -> dict[str, Any]:
    d = run.decisions
    row = d[d["bar_ts"] == ts]
    if row.empty:
        raise KeyError(f"no risk decision for {ts}")
    r = row.iloc[-1]
    spec = scenario(run.scenario)
    return {
        "scenario": run.scenario,
        "sizing_method": spec.sizing.method,
        "target_volatility": spec.sizing.target_volatility
        if spec.sizing.method == "volatility_target"
        else None,
        "atr_risk_budget": spec.sizing.atr_risk_budget
        if spec.sizing.method == "atr_risk"
        else None,
        "stop_method": spec.stop.method,
        "drawdown_lock_enabled": spec.drawdown.enabled,
        **{str(k): (None if isinstance(v, float) and np.isnan(v) else v) for k, v in r.items()},
    }


def risk_scenario_table(bars: pd.DataFrame, b: EngineBundle, strategy_id: str) -> pd.DataFrame:
    rows = []
    for s in SCENARIOS:
        r = risk_run(bars, b, strategy_id, s.name)
        rows.append(
            {
                "scenario": s.name,
                **{
                    k: r.metrics[k]
                    for k in (
                        "cumulative_return",
                        "cagr",
                        "annualized_volatility",
                        "sharpe",
                        "max_drawdown",
                        "exposure",
                        "annualized_turnover",
                        "total_costs",
                    )
                },
                "interventions": r.diagnostics["interventions"],
                "stop_triggers": r.diagnostics["stop_triggers"],
                "drawdown_lock_events": r.diagnostics["drawdown_lock_events"],
                "fingerprint": s.fingerprint(),
            }
        )
    return pd.DataFrame(rows)


def scenario_specs() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "scenario": s.name,
                **{f"sizing.{k}": v for k, v in asdict(s.sizing).items()},
                "stop": s.stop.method,
                "drawdown_lock": s.drawdown.enabled,
                "rebalance_band": s.rebalance_band,
            }
            for s in SCENARIOS
        ]
    )
