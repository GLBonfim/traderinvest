"""Train challenger models and record replacements using purged expanding walk-forward folds.

All available IBKR history has already been inspected. Even walk-forward results here are
RETROSPECTIVE research, not a new untouched holdout. Selected models are experimental PAPER
candidates only; the existing statistically approved agent set is never changed by this tool.
"""

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.linear_model import LogisticRegression, Ridge

from app.fx.agents.trained import (
    BAR_HOURS,
    FEATURE_NAMES,
    RETIREMENT_POLICY,
    RUN_SCHEMA,
    ForestModel,
    LinearModel,
    feature_frame,
    model_signals,
)
from app.fx.models import PAIRS, pair
from app.fx.research.backtest import DEFAULT_COSTS, run_backtest, summarize
from app.fx.research.history import DATA_DIR, load_pair, resample, with_spread_floor
from app.fx.research.study import agent_trades, load_all, one_sided_bootstrap_p
from app.validation.comparisons import holm

OUT = Path("data/fx/training")
FOLDS = (
    ("2023", "2023-01-01", "2024-01-01"),
    ("2024", "2024-01-01", "2025-01-01"),
    ("2025", "2025-01-01", "2026-01-01"),
)
FINAL_TRAIN_END = "2026-10-17T00:00:00+00:00"
HORIZON = 8
RECIPES = [
    (tf, algo, alpha)
    for tf in BAR_HOURS
    for algo, strengths in (("ridge", (1.0, 100.0)), ("logistic", (0.1, 1.0)))
    for alpha in strengths
]
POLICY: dict[str, Any] = {
    "folds": FOLDS,
    "recipes": RECIPES,
    "purge_bars": HORIZON + 1,
    "embargo_hours": 24,
    "min_trades": 100,
    "min_positive_folds": 2,
    "min_profit_factor": 1.05,
    "min_mean_r": 0.0,
    "costs": asdict(DEFAULT_COSTS),
    "seed": 7,
    "approval": "retrospective only; paper forward evidence is still required",
    "feature_names": FEATURE_NAMES,
    "ridge_threshold_atr": 0.35,
    "logistic_direction_threshold": 0.60,
    "stop_atr": 2.0,
    "target_atr": 3.0,
    "max_hold_bars": HORIZON,
    "paper_retirement": RETIREMENT_POLICY,
}


def training_rows(
    frames: dict[str, pd.DataFrame],
    timeframe: str,
    before: pd.Timestamp,
) -> tuple[np.ndarray, np.ndarray]:
    """Purge every label whose EXIT could touch a validation period; exclude weekend labels."""
    xs, ys = [], []
    step = pd.Timedelta(hours=BAR_HOURS[timeframe])
    for df in frames.values():
        mid = mid_frame(df)
        f = feature_frame(mid, timeframe)
        entry = mid["open"].shift(-1)
        exit_price = mid["close"].shift(-HORIZON)
        y = ((exit_price - entry) / f["atr"]).clip(-20, 20)
        index = pd.Series(pd.DatetimeIndex(df.index), index=df.index)
        label_end = index.shift(-HORIZON) + step
        no_gap = label_end - index == (HORIZON + 1) * step
        mask = (label_end < before - pd.Timedelta(hours=24)) & no_gap
        x = f[list(FEATURE_NAMES)].to_numpy(float)
        valid = mask.to_numpy(bool) & np.isfinite(x).all(axis=1) & np.isfinite(y.to_numpy())
        xs.append(x[valid])
        ys.append(y.to_numpy(float)[valid])
    joined_x, joined_y = np.concatenate(xs), np.concatenate(ys)
    if len(joined_y) < 500:
        raise ValueError("insufficient purged training observations")
    return joined_x, joined_y


def mid_frame(df: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {k: (df[f"bid_{k}"] + df[f"ask_{k}"]) / 2 for k in ("open", "high", "low", "close")},
        index=df.index,
    )


def fit_model(
    frames: dict[str, pd.DataFrame],
    timeframe: str,
    algorithm: str,
    strength: float,
    cutoff: pd.Timestamp,
    threshold_override: float | None = None,
) -> LinearModel:
    x, y = training_rows(frames, timeframe, cutoff)
    center, scale = x.mean(axis=0), x.std(axis=0)
    scale = np.where(scale > 1e-8, scale, 1.0)
    z = (x - center) / scale
    if algorithm == "ridge":
        fitted = Ridge(alpha=strength).fit(z, y)
        coef, intercept = fitted.coef_, float(fitted.intercept_)
        threshold = 0.35  # fixed BEFORE evaluating any fold
    elif algorithm == "logistic":
        fitted_logistic = LogisticRegression(C=strength, max_iter=300, random_state=7).fit(z, y > 0)
        coef, intercept = fitted_logistic.coef_[0], float(fitted_logistic.intercept_[0])
        threshold = 0.60  # directional class score, NOT win probability
    else:
        forest = ExtraTreesRegressor(
            n_estimators=48,
            max_depth=5,
            min_samples_leaf=int(strength),
            max_features=0.8,
            n_jobs=1,
            random_state=7,
        ).fit(z, y)
        trees = tuple(
            {
                "left": t.tree_.children_left.tolist(),
                "right": t.tree_.children_right.tolist(),
                "feature": t.tree_.feature.tolist(),
                "threshold": t.tree_.threshold.tolist(),
                "value": t.tree_.value[:, 0, 0].tolist(),
            }
            for t in forest.estimators_
        )
        return ForestModel(
            f"forest-{timeframe}-{strength:g}-t{threshold_override or 0.2:g}",
            "forest",
            timeframe,
            tuple(center),
            tuple(scale),
            (0.0,) * len(FEATURE_NAMES),
            0.0,
            threshold_override or 0.2,
            cutoff.isoformat(),
            trees=trees,
        )
    threshold = threshold if threshold_override is None else threshold_override
    return LinearModel(
        f"{algorithm}-{timeframe}-{strength:g}-t{threshold:g}",
        algorithm,
        timeframe,
        tuple(center),
        tuple(scale),
        tuple(coef),
        intercept,
        threshold,
        cutoff.isoformat(),
    )


def evaluate(
    frames: dict[str, pd.DataFrame],
    model: LinearModel,
    lo: pd.Timestamp,
    hi: pd.Timestamp,
    stressed: bool = False,
) -> pd.DataFrame:
    trades = []
    costs = (
        replace(
            DEFAULT_COSTS, commission_bps=0.4, financing_bps_per_night=1.0, extra_slippage_pips=0.2
        )
        if stressed
        else DEFAULT_COSTS
    )
    for symbol, df in frames.items():
        sig = model_signals(mid_frame(df), model)
        mask = (df.index >= lo) & (df.index < hi)
        spread = (df["ask_close"] - df["bid_close"]) / pair(symbol).pip_size
        allowed = spread <= 3.0
        result = run_backtest(
            df.loc[mask],
            sig.loc[mask],
            symbol,
            allowed.loc[mask],
            costs,
            gap=timedelta(hours=BAR_HOURS[model.timeframe] * 2),
        )
        if len(result):
            trades.append(result)
    if not trades:
        return pd.DataFrame()
    return pd.concat(trades, ignore_index=True).sort_values("entry_ts", ignore_index=True)


def digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, allow_nan=False).encode()).hexdigest()


def strict_json(value: Any) -> Any:
    """Non-finite descriptive metrics become null; model coefficients are always finite."""
    if isinstance(value, dict):
        return {k: strict_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [strict_json(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


def run(
    root: Path, out: Path, symbols: list[str], expanded: bool = False, nonlinear: bool = False
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    recipes = [
        (tf, algo, strength, threshold)
        for tf, algo, strength in RECIPES
        for threshold in ((0.10, 0.20, 0.35) if algo == "ridge" else (0.52, 0.55, 0.60))
        if expanded or threshold == (0.35 if algo == "ridge" else 0.60)
    ]
    if nonlinear:
        recipes += [
            (tf, "forest", leaf, threshold)
            for tf in BAR_HOURS
            for leaf in (100.0, 300.0)
            for threshold in (0.10, 0.20, 0.35)
        ]
    protocol = {
        "created_at": datetime.now(UTC).isoformat(),
        "policy": {
            **POLICY,
            "recipes": recipes,
            "expanded_search_after_initial_result": expanded,
            "nonlinear_followup": nonlinear,
            "forest": {"estimators": 48, "depth": 5, "features_fraction": 0.8, "seed": 7},
        },
        "symbols": symbols,
        "research_status": "retrospective_only",
        "input_manifest_sha256": (
            hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
            if (root / "manifest.json").exists()
            else None
        ),
    }
    protocol["sha256"] = digest(protocol)
    protocol_path = out / "protocol.json"
    if protocol_path.exists():
        raise FileExistsError(
            "use a new output folder for every tournament; never overwrite results"
        )
    protocol_path.write_text(json.dumps(protocol, indent=2), encoding="utf-8")
    raw = {s: with_spread_floor(load_pair(root, s), s) for s in symbols}
    by_tf = {
        tf: {s: resample(df, f"{hours}h") for s, df in raw.items()}
        for tf, hours in BAR_HOURS.items()
    }
    # Incumbent parameters were already inspected/tuned: descriptive comparison only.
    from app.fx.agents import calibrated

    old_data = load_all(root, symbols)
    incumbents = {}
    for role, params in (
        ("trend", calibrated.TREND),
        ("momentum", calibrated.MOMENTUM),
        ("mean_reversion", calibrated.MEAN_REVERSION),
    ):
        old_trades = agent_trades(old_data, role, params)
        if len(old_trades):
            old_trades = old_trades[
                (old_trades["entry_ts"] >= pd.Timestamp("2023-01-01", tz="UTC"))
                & (old_trades["entry_ts"] < pd.Timestamp("2026-01-01", tz="UTC"))
            ]
        incumbents[role] = summarize(old_trades)
    del old_data
    results: list[dict[str, Any]] = []
    final_models: list[LinearModel] = []
    for timeframe, algorithm, strength, threshold in recipes:
        frames = by_tf[timeframe]
        parts: list[pd.DataFrame] = []
        stress_parts: list[pd.DataFrame] = []
        folds: list[dict[str, Any]] = []
        for label, start, end in FOLDS:
            lo, hi = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
            model = fit_model(frames, timeframe, algorithm, strength, lo, threshold)
            t = evaluate(frames, model, lo, hi)
            stress = evaluate(frames, model, lo, hi, stressed=True)
            folds.append({"fold": label, "summary": summarize(t), "trained_before": lo.isoformat()})
            if len(t):
                parts.append(t)
            if len(stress):
                stress_parts.append(stress)
        combined = (
            pd.concat(parts, ignore_index=True).sort_values("entry_ts") if parts else pd.DataFrame()
        )
        stress_all = pd.concat(stress_parts, ignore_index=True) if stress_parts else pd.DataFrame()
        summary, stress_summary = summarize(combined), summarize(stress_all)
        returns = combined["r_net"].to_numpy(float) if len(combined) else np.array([])
        positive = sum(f["summary"]["mean_r"] > 0 for f in folds)
        model = fit_model(
            frames, timeframe, algorithm, strength, pd.Timestamp(FINAL_TRAIN_END), threshold
        )
        final_models.append(model)
        p = one_sided_bootstrap_p(returns)
        result: dict[str, Any] = {
            "model_id": model.model_id,
            "folds": folds,
            "summary": summary,
            "stressed": stress_summary,
            "positive_folds": positive,
            "p_value": float(p) if np.isfinite(p) else 1.0,
        }
        result["paper_candidate"] = bool(
            summary["trades"] >= POLICY["min_trades"]
            and summary["mean_r"] > 0
            and summary["profit_factor"] >= POLICY["min_profit_factor"]
            and positive >= POLICY["min_positive_folds"]
            and stress_summary["mean_r"] > 0
        )
        results.append(result)
        print(json.dumps(result, default=str), flush=True)
    adjusted = holm(np.array([r["p_value"] for r in results]))
    for result, p in zip(results, adjusted, strict=True):
        result["p_holm"] = float(p)
        result["retrospective_evidence"] = bool(result["paper_candidate"] and p < 0.1)
    ranked = sorted(results, key=lambda r: r["summary"]["mean_r"], reverse=True)
    selected: list[str] = []
    seen_families: set[tuple[str, str]] = set()
    by_id = {m.model_id: m for m in final_models}
    for result in ranked:
        candidate = by_id[result["model_id"]]
        family = (candidate.algorithm, candidate.timeframe)
        if result["paper_candidate"] and family not in seen_families and len(selected) < 3:
            selected.append(candidate.model_id)
            seen_families.add(family)
    slots = ("trend", "momentum", "mean_reversion")
    replacements = [
        {
            "slot": slot,
            "old": "v2 rejected in prior holdout",
            "replacement": selected[i] if i < len(selected) else "no_trade",
            "reason": "retrospective positive net/stress folds"
            if i < len(selected)
            else "no challenger passed the predeclared paper-candidate gate",
        }
        for i, slot in enumerate(slots)
    ]
    registry = {
        "schema": RUN_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(),
        "protocol_sha256": protocol["sha256"],
        "statistically_approved": False,
        "research_status": "retrospective_only",
        "active_model_ids": selected,
        "models": [m.to_dict() for m in final_models],
        "ranking": ranked,
        "replacements": replacements,
        "incumbents_descriptive_same_years": incumbents,
        "no_trade_benchmark_mean_r": 0.0,
        "retirement_policy": RETIREMENT_POLICY,
        "forward_paper_required": True,
    }
    (out / "selected.json").write_text(
        json.dumps(strict_json(registry), indent=2, allow_nan=False), encoding="utf-8"
    )
    lines = [
        "# Treinamento e substituição de agentes",
        "",
        "Pesquisa retrospectiva: todo o histórico já foi inspecionado. Nenhum resultado "
        "aqui é nova validação independente. Candidatos exigem observação prospectiva paper.",
        "",
        "O protocolo e a busca foram gravados antes dos resultados; treino expanding "
        "até 2023/2024/2025, rótulos purgados e embargo de 24h. Custos e execução bid/ask.",
        "",
        "| Modelo | Operações | Média R líquida | PF | Anos positivos | R com custos "
        "agravados | Candidato paper |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in ranked:
        s = r["summary"]
        lines.append(
            f"| {r['model_id']} | {s['trades']:.0f} | {s['mean_r']:+.4f} | "
            f"{s['profit_factor']:.3f} | {r['positive_folds']}/3 | "
            f"{r['stressed']['mean_r']:+.4f} | {r['paper_candidate']} |"
        )
    lines += ["", "## Registro de substituição", ""]
    lines += [f"- {r['slot']}: {r['replacement']} ({r['reason']})." for r in replacements]
    lines += [
        "",
        "Nenhum candidato perdedor é promovido para preencher vagas. `no_trade` "
        "preserva o capital virtual enquanto não houver candidato elegível.",
        "",
    ]
    lines += [
        "## Agentes anteriores (comparação descritiva nos mesmos anos)",
        "",
        "Parâmetros anteriores já conhecidos; isto não é uma nova validação dos incumbentes.",
        "",
        "| Agente | Operações | Média R líquida | PF |",
        "|---|---|---|---|",
    ]
    for role, summary in incumbents.items():
        lines.append(
            f"| {role} | {summary['trades']:.0f} | {summary['mean_r']:+.4f} | "
            f"{summary['profit_factor']:.3f} |"
        )
    lines += [
        "",
        "No piloto, uma queda realizada de 5 R desde o pico retira o candidato. "
        "Após 20 operações, média líquida negativa e PF abaixo de 1 nas últimas 50 "
        "também provocam substituição por outro candidato elegível, ou NO TRADE.",
        "",
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return registry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--pairs", nargs="+", default=list(PAIRS))
    parser.add_argument(
        "--expanded",
        action="store_true",
        help="record a 24-candidate retrospective search including lower gates",
    )
    parser.add_argument(
        "--nonlinear", action="store_true", help="add 12 shallow forest challengers"
    )
    args = parser.parse_args()
    if not set(args.pairs) <= set(PAIRS) or len(set(args.pairs)) != len(args.pairs):
        parser.error("unknown or duplicate pairs")
    registry = run(args.data, args.out, args.pairs, args.expanded, args.nonlinear)
    print("experimental paper candidates:", registry["active_model_ids"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
