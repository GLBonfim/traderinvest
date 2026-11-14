"""Offline-only M5 training with causal candle and Aula02 features. No broker imports."""

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor
from sklearn.linear_model import LogisticRegression, Ridge

from app.candles.lesson_m5 import allowed as complete_sequence
from app.fx.agents.trained import FEATURE_NAMES, feature_frame
from app.fx.models import pair
from app.fx.research.backtest import EUR_TO, USD_TO, _trading_day_ordinal
from app.fx.research.m5_validation import (
    BAR,
    PROTOCOL,
    ROLES,
    analyst_frame,
    check_manifest,
    fee_floor,
    gate,
    load_m5,
    mid_of,
    pattern_frame,
    save_trades,
    simulate,
    write_json,
)

VERSION = "m5-aula-context-training/1.1.0"
SOURCE_SHA = "4e53c8c39e0944751180398927e13ea31e4dcd2f1eb7143fc3a960971804fa1c"
EXTRA = (
    "h4_bias",
    "h4_position",
    "upper_sweep",
    "mss_bear",
    "fvg_bear",
    "ote_fraction",
    "ote_zone",
    "complete_crt_ote",
    "h4_available",
    "reversal_bear",
    "continuation_bear",
    "momentum_bear",
)
NAMES = tuple(FEATURE_NAMES) + EXTRA


def lesson_features(mid: pd.DataFrame, patterns: pd.DataFrame) -> pd.DataFrame:
    """Previous COMPLETE UTC H4 only; current M5 bar is closed before use.

    OTE fraction uses the previous twelve M5 bars as a fixed local expansion proxy.
    It is a feature, not an assertion that the full illustrated setup has occurred.
    """
    idx = pd.DatetimeIndex(mid.index)
    if idx.tz is None or not idx.is_monotonic_increasing or not idx.is_unique:
        raise ValueError("ordered unique timezone-aware M5 required")
    utc = mid.copy()
    utc.index = idx.tz_convert("UTC")
    if not ((utc.index.minute % 5 == 0) & (utc.index.second == 0)).all():
        raise ValueError("native five-minute grid required")
    grouped = utc.resample("4h")
    h4 = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    h4 = h4.loc[grouped.close.count() == 48]
    ref = h4.reindex(utc.index.floor("4h") - pd.Timedelta(hours=4))
    ref.index = mid.index
    width = (ref.high - ref.low).replace(0, np.nan)
    previous_low = mid.low.rolling(3).min().shift(1)
    prior_high = mid.high.rolling(12).max().shift(1)
    prior_low = mid.low.rolling(12).min().shift(1)
    frac = (mid.close - prior_low) / (prior_high - prior_low).replace(0, np.nan)
    out = pd.DataFrame(index=mid.index)
    out["h4_bias"] = (ref.close - ref.open) / width
    out["h4_position"] = (mid.close - ref.low) / width
    out["upper_sweep"] = (
        (mid.high > ref.high) & (mid.close < ref.high) & (mid.close > ref.low)
    ).astype(float)
    out["mss_bear"] = ((mid.close < previous_low) & (mid.close < mid.open)).astype(float)
    out["fvg_bear"] = (mid.high < mid.low.shift(2)).astype(float)
    out["ote_fraction"] = frac
    out["ote_zone"] = frac.between(0.62, 0.79).astype(float)
    out["complete_crt_ote"] = complete_sequence(mid).astype(float)
    out["h4_available"] = ref.high.notna().astype(float)
    for family in ("reversal", "continuation", "momentum"):
        out[family + "_bear"] = patterns[family + "_bear"].to_numpy(float)
    return out.replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(-20, 20)


def research_mask(
    f: pd.DataFrame, patterns: pd.DataFrame, lesson: pd.DataFrame, role: str
) -> np.ndarray:
    family = ROLES[role][0]
    a = analyst_frame(f)
    bear = patterns[family + "_bear"].to_numpy(float)
    bull = patterns[family + "_bull"].to_numpy(float)
    result = a.tradable.to_numpy(bool) & (bear >= PROTOCOL.min_pattern_strength)
    result &= lesson.h4_available.to_numpy(bool) & (lesson.h4_bias.to_numpy(float) < 0)
    if role == "reversao":
        result &= (bear > bull) & (a.efficiency.to_numpy(float) <= PROTOCOL.max_reversal_efficiency)
    else:
        result &= a.trend.to_numpy(int) < 0
    return result


def labels(df: pd.DataFrame, f: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Independent hypothetical SELL outcome per bar, NOT executed portfolio trades.

    Next BID open entry, ASK stop/target/time exit, 96-bar horizon. Opening
    gaps precede later intrabar touches; otherwise stop wins intrabar ties.
    Future candles are used ONLY here as supervised targets, never as input features.
    Gaps/missing next bar and incomplete horizons are excluded.
    """
    h = PROTOCOL.horizon_bars
    result = pd.DataFrame({"net_r": np.nan, "label_end": pd.NaT}, index=df.index)
    result["label_end"] = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns, UTC]")
    index = pd.Series(df.index, index=df.index)
    end = index.shift(-h) + BAR
    contiguous = (end - index) == (h + 1) * BAR
    stop_dist = np.maximum(PROTOCOL.stop_atr * f.atr.to_numpy(float), fee_floor(symbol))
    entry_all = df.bid_open.shift(-1).to_numpy(float)
    quote = pair(symbol).quote
    risk_budget = (
        PROTOCOL.base_costs.capital_eur * PROTOCOL.base_costs.risk_pct / 100 * EUR_TO.get(quote, 1)
    )
    units = np.minimum(risk_budget / stop_dist, PROTOCOL.base_costs.max_units)
    units = np.floor(units / 1000) * 1000
    min_q = PROTOCOL.base_costs.commission_min_usd * USD_TO.get(quote, 1)
    fee = 2 * np.maximum(min_q, PROTOCOL.base_costs.commission_bps * 1e-4 * units * entry_all)
    valid = contiguous.to_numpy(bool) & np.isfinite(stop_dist) & (units >= 1000)
    valid &= fee / np.maximum(units * stop_dist, 1e-12) <= PROTOCOL.max_fee_risk + 1e-9
    candidates = np.flatnonzero(valid)
    ask_hi, ask_lo, ask_close = (df["ask_" + k].to_numpy(float) for k in ("high", "low", "close"))
    ask_open = df.ask_open.to_numpy(float)
    trading_days = _trading_day_ordinal(pd.DatetimeIndex(df.index))
    # Batches cap memory; opening gaps are ordered before intrabar barriers.
    # When only the intrabar order is unknown, the stop retains precedence.
    net_values = np.full(len(df), np.nan)
    for first in range(0, len(candidates), 2048):
        j = candidates[first : first + 2048]
        offsets = j[:, None] + 1 + np.arange(h)[None, :]
        stop, target = entry_all[j] + stop_dist[j], entry_all[j] - 2 * stop_dist[j]
        hit_stop = ask_hi[offsets] >= stop[:, None]
        hit_target = ask_lo[offsets] <= target[:, None]
        opening_stop = ask_open[offsets] >= stop[:, None]
        opening_target = ask_open[offsets] <= target[:, None]
        any_hit = opening_stop | opening_target | hit_stop | hit_target
        at = np.where(any_hit.any(axis=1), any_hit.argmax(axis=1), h - 1)
        row = np.arange(len(j))
        exit_index = j + 1 + at
        gap_stop = opening_stop[row, at]
        gap_target = ~gap_stop & opening_target[row, at]
        opening_exit = gap_stop | gap_target
        stop_first = ~opening_exit & hit_stop[row, at]
        target_first = ~opening_exit & ~stop_first & hit_target[row, at]
        px = ask_close[exit_index].copy()
        px[opening_exit] = ask_open[exit_index[opening_exit]]
        px[stop_first] = stop[stop_first]
        px[target_first] = target[target_first]
        commission = np.maximum(
            min_q, PROTOCOL.base_costs.commission_bps * 1e-4 * units[j] * entry_all[j]
        )
        commission += np.maximum(min_q, PROTOCOL.base_costs.commission_bps * 1e-4 * units[j] * px)
        nights = np.maximum(0, trading_days[exit_index] - trading_days[j + 1])
        financing = (
            nights * PROTOCOL.base_costs.financing_bps_per_night * 1e-4 * units[j] * entry_all[j]
        )
        net = (entry_all[j] - px) / stop_dist[j] - (commission + financing) / (
            units[j] * stop_dist[j]
        )
        net_values[j] = net
    # Full horizon end, not early exit, is used for conservative purging at fold boundaries.
    result["net_r"] = net_values
    result["label_end"] = end
    return result


def purged_mask(index: pd.DatetimeIndex, target: pd.DataFrame, before: pd.Timestamp) -> np.ndarray:
    cutoff = before - pd.Timedelta(hours=PROTOCOL.embargo_hours)
    return np.asarray(
        (index >= pd.Timestamp(PROTOCOL.train_start))
        & (target.label_end < cutoff)
        & target.net_r.notna(),
        dtype=bool,
    )


def fitted_model(
    role: str, variant: float, x: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None
) -> tuple[Any, dict[str, Any]]:
    if x.ndim != 2 or x.shape[1] != len(NAMES):
        raise ValueError("training feature dimensions/version differ")
    center, scale = x.mean(axis=0), x.std(axis=0)
    scale = np.where(scale > 1e-8, scale, 1.0)
    z = (x - center) / scale
    if role == "reversao":
        model = Ridge(alpha=variant).fit(z, y, sample_weight=sample_weight)
    elif role == "continuacao":
        model = ExtraTreesRegressor(
            n_estimators=32,
            max_depth=4,
            min_samples_leaf=int(variant),
            max_features=0.8,
            n_jobs=1,
            random_state=7,
        ).fit(z, y, sample_weight=sample_weight)
    else:
        if len(np.unique(y > 0)) != 2:
            raise ValueError("training requires profitable and unprofitable labels")
        model = LogisticRegression(C=variant, max_iter=1000, random_state=7).fit(
            z, (y > 0).astype(int), sample_weight=sample_weight
        )
    artifact: dict[str, Any] = {
        "schema": VERSION,
        "feature_names": NAMES,
        "center": center.tolist(),
        "scale": scale.tolist(),
        "role": role,
        "variant": variant,
        "training_rows": len(y),
        "training_positive_labels": int((y > 0).sum()),
        "offline_only": True,
        "execution_enabled": False,
        "statistically_approved": False,
    }
    if role == "continuacao":
        artifact["trees"] = [
            {
                "left": t.tree_.children_left.tolist(),
                "right": t.tree_.children_right.tolist(),
                "feature": t.tree_.feature.tolist(),
                "threshold": t.tree_.threshold.tolist(),
                "value": t.tree_.value[:, 0, 0].tolist(),
            }
            for t in model.estimators_
        ]
    else:
        artifact["weights"] = model.coef_.tolist()
        artifact["intercept"] = np.asarray(model.intercept_).tolist()
    return (model, center, scale), artifact


def score(fit: Any, x: np.ndarray, role: str) -> np.ndarray:
    model, center, scale = fit
    z = (x - center) / scale
    return np.asarray(
        model.predict_proba(z)[:, 1] if role == "impulso" else model.predict(z), dtype=float
    )


def signals(
    df: pd.DataFrame,
    f: pd.DataFrame,
    mask: np.ndarray,
    forecast: np.ndarray,
    role: str,
    symbol: str,
) -> pd.DataFrame:
    threshold = 0.55 if role == "impulso" else 0.10
    spread = (df.ask_close - df.bid_close).to_numpy(float) / pair(symbol).pip_size
    permitted = (
        mask & np.isfinite(forecast) & (forecast > threshold) & (spread <= PROTOCOL.max_spread_pips)
    )
    stop = np.maximum(PROTOCOL.stop_atr * f.atr.to_numpy(float), fee_floor(symbol))
    return pd.DataFrame(
        {
            "dir": np.where(permitted, -1, 0),
            "stop_dist": stop,
            "target_dist": 2 * stop,
            "max_hold": 96,
            "score": np.clip(np.nan_to_num(forecast), 0, 1),
        },
        index=df.index,
    )


def frozen_protocol(source: Path) -> dict[str, Any]:
    actual = hashlib.sha256(source.read_bytes()).hexdigest()
    if actual != SOURCE_SHA:
        raise ValueError("Aula02 source hash differs; do not silently change curriculum")
    return {
        "schema": VERSION,
        "source_file": source.name,
        "training_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha256": actual,
        "source_pages": {
            "htf_ltf": [9, 24],
            "ote": [14],
            "sessions": [15, 16, 17, 18, 19, 20, 21],
            "crt": [24, 25, 26, 27, 28],
        },
        "base_protocol": asdict(PROTOCOL),
        "features": NAMES,
        "new_hypothesis": (
            "Aula02 features inside supervised net-of-cost learning, not the earlier "
            "mandatory rare CRT/OTE veto. Strong same-family candle, bearish complete H4 "
            "context, analyst and learned SELL profitability must agree. Not a promotion or "
            "relaxation of previous results."
        ),
        "thresholds": {"net_r_regression": 0.10, "positive_sell_probability": 0.55},
        "training_target": (
            "Independent hypothetical SELL realized net R, next BID open, ASK exits; "
            "stop-first ties; horizon96; same size and fee floor; full-horizon purge "
            "plus24h embargo."
        ),
        "variants": {
            "reversao": [1.0, 100.0],
            "continuacao": [200.0, 1000.0],
            "impulso": [0.1, 1.0],
        },
        "frozen_before_data": True,
        "development_validation_years_already_seen": [2024, 2025],
        "unseen_test_claim": False,
        "holdout_2026_used": False,
        "execution_enabled": False,
        "minimum_training_examples_per_specialist": 300,
        "curriculum_sampling": (
            "All valid bearish-complete-H4 background examples; matching role setup "
            "sample_weight=5, other background weight=1. Validation still requires "
            "original role setup, analyst, costs and model agreement."
        ),
        "reason_new_version": (
            "Prior attempt had only71 continuation rows and aborted; broaden training "
            "context, not validation acceptance; no automatic search beyond six declared "
            "candidates."
        ),
        "promotion": (
            "No automatic export to live registry or paper activation. Joint "
            "risk/cash/execution validation still required after any future independent "
            "acceptance."
        ),
    }


def run(
    data: Path,
    out: Path,
    source: Path,
    *,
    fit_function: Any = None,
    score_function: Any = None,
    protocol_function: Any = None,
    pattern_function: Any = None,
    feature_function: Any = None,
    source_files: dict[str, Path] | None = None,
    sample_weight_function: Any = None,
) -> None:
    if out.exists():
        raise ValueError("run directory exists; frozen results must not be overwritten")
    trainer = fitted_model if fit_function is None else fit_function
    predictor = score if score_function is None else score_function
    pattern_builder = pattern_frame if pattern_function is None else pattern_function

    def sample_weights(symbol: str, eligible: np.ndarray, mask: np.ndarray) -> np.ndarray:
        weights = np.where(mask[eligible], 5.0, 1.0)
        if sample_weight_function is not None:
            weights = sample_weight_function(frames[symbol].index, ys[symbol], eligible, weights)
        return weights

    proto = frozen_protocol(source) if protocol_function is None else protocol_function(source)
    out.mkdir(parents=True)
    protocol_sha = write_json(out / "protocol.json", proto)
    (out / "training_source.py").write_bytes(Path(__file__).read_bytes())
    for name, path in (source_files or {}).items():
        if Path(name).name != name:
            raise ValueError("archive source must be a basename")
        (out / name).write_bytes(path.read_bytes())
    print(f"Frozen protocol {protocol_sha}", flush=True)
    write_json(out / "data_manifest.json", check_manifest(data))
    frames = {s: load_m5(data, s, pd.Timestamp(PROTOCOL.holdout_start)) for s in PROTOCOL.pairs}
    fs, ps, ls, xs, ys = {}, {}, {}, {}, {}
    diagnostics: dict[str, Any] = {}
    for symbol, df in frames.items():
        mid = mid_of(df)
        fs[symbol] = feature_frame(mid, "M5")
        ps[symbol] = pattern_builder(mid)
        ls[symbol] = lesson_features(mid, ps[symbol])
        xs[symbol] = pd.concat([fs[symbol][list(FEATURE_NAMES)], ls[symbol]], axis=1).to_numpy(
            float
        )
        if feature_function is not None:
            xs[symbol] = feature_function(df, fs[symbol], symbol, xs[symbol])
        ys[symbol] = labels(df, fs[symbol], symbol)
        diagnostics[symbol] = {
            "bars": len(df),
            "from": str(df.index.min()),
            "to": str(df.index.max()),
            "sweeps": int(ls[symbol].upper_sweep.sum()),
            "mss": int(ls[symbol].mss_bear.sum()),
            "complete_crt_ote": int(ls[symbol].complete_crt_ote.sum()),
            "potential_valid_labels": int(ys[symbol].net_r.notna().sum()),
            "role_opportunities": {
                r: int(research_mask(fs[symbol], ps[symbol], ls[symbol], r).sum()) for r in ROLES
            },
        }
        print(f"Prepared {symbol}: {diagnostics[symbol]}", flush=True)
        write_json(out / "diagnostics.json", diagnostics)
    results = []
    for role in ROLES:
        masks = {s: research_mask(fs[s], ps[s], ls[s], role) for s in frames}
        for variant in proto["variants"][role]:
            candidate = f"{role}-{variant:g}"
            periods, stressed, fold_details = {}, [], []
            for name, start, end in PROTOCOL.folds:
                lo, hi = pd.Timestamp(start), pd.Timestamp(end)
                eligible = {
                    s: (ls[s].h4_available.to_numpy(bool) & (ls[s].h4_bias.to_numpy(float) < 0))
                    & purged_mask(pd.DatetimeIndex(frames[s].index), ys[s], lo)
                    & np.isfinite(xs[s]).all(axis=1)
                    for s in frames
                }
                x = np.concatenate([xs[s][eligible[s]] for s in frames])
                y = np.concatenate([ys[s].net_r.to_numpy(float)[eligible[s]] for s in frames])
                if len(y) < 300:
                    raise ValueError(f"{role}: insufficient purged lesson examples ({len(y)})")
                weights = np.concatenate([sample_weights(s, eligible[s], masks[s]) for s in frames])
                fit, artifact = trainer(role, variant, x, y, sample_weight=weights)
                artifact["specialty_weighted_examples"] = int((weights > 1).sum())
                artifact.update(training_end=lo.isoformat(), protocol_sha256=protocol_sha)
                model_path = out / "models" / f"{candidate}-fold-{name}.json"
                model_path.parent.mkdir(exist_ok=True)
                sha = write_json(model_path, artifact)
                ss = {
                    s: signals(
                        frames[s],
                        fs[s],
                        masks[s],
                        predictor(fit, np.nan_to_num(xs[s]), role),
                        role,
                        s,
                    )
                    for s in frames
                }
                periods[name] = simulate(frames, ss, lo, hi, PROTOCOL.base_costs)
                stress = simulate(frames, ss, lo, hi, PROTOCOL.stress_costs)
                stressed.append(stress)
                fold_details.append(
                    {
                        "fold": name,
                        "training_rows": len(y),
                        "positive_training_labels": int((y > 0).sum()),
                        "model_sha256": sha,
                        "validation_trades": len(periods[name]),
                        "base_csv_sha256": save_trades(
                            out / "trades", candidate + "-" + name + "-base", periods[name]
                        ),
                        "stress_csv_sha256": save_trades(
                            out / "trades", candidate + "-" + name + "-stress", stress
                        ),
                    }
                )
                print(
                    f"Trained {candidate} {name}: rows={len(y)}, "
                    f"validation_trades={len(periods[name])}",
                    flush=True,
                )
            evaluation = gate(periods, pd.concat(stressed, ignore_index=True))
            results.append(
                {
                    "candidate": candidate,
                    "role": role,
                    "variant": variant,
                    "folds": fold_details,
                    **evaluation,
                }
            )
            write_json(out / "candidates.json", results)
            print(f"{candidate}: gate={evaluation['passed']}", flush=True)
    # Save development-final weights even if rejected, clearly segregated from approved registries.
    for role in ROLES:
        best = max((c for c in results if c["role"] == role), key=lambda c: c["pooled"]["mean_r"])
        eligible = {
            s: (ls[s].h4_available.to_numpy(bool) & (ls[s].h4_bias.to_numpy(float) < 0))
            & purged_mask(
                pd.DatetimeIndex(frames[s].index), ys[s], pd.Timestamp(PROTOCOL.holdout_start)
            )
            & np.isfinite(xs[s]).all(axis=1)
            for s in frames
        }
        x = np.concatenate([xs[s][eligible[s]] for s in frames])
        y = np.concatenate([ys[s].net_r.to_numpy(float)[eligible[s]] for s in frames])
        weights = np.concatenate(
            [
                sample_weights(s, eligible[s], research_mask(fs[s], ps[s], ls[s], role))
                for s in frames
            ]
        )
        _, artifact = trainer(role, best["variant"], x, y, sample_weight=weights)
        artifact["specialty_weighted_examples"] = int((weights > 1).sum())
        artifact.update(
            training_end=PROTOCOL.holdout_start,
            protocol_sha256=protocol_sha,
            selection_basis="development folds already seen",
            validation_passed=best["passed"],
            requires_fresh_holdout_and_joint_validation=True,
        )
        write_json(out / "models" / f"{role}-development-final.json", artifact)
    summary = {
        "completed_at": datetime.now(UTC).isoformat(),
        "protocol_sha256": protocol_sha,
        "models_trained": len(list((out / "models").glob("*.json"))),
        "specialists": list(ROLES),
        "passed_development_candidates": sum(c["passed"] for c in results),
        "holdout_assessed": False,
        "paper_activated": False,
        "historically_approved": False,
        "broker_imported": False,
        "orders_sent": 0,
        "diagnostics": diagnostics,
    }
    write_json(out / "summary.json", summary)
    lines = [
        "# Treino offline M5 com histÃ³rico e Aula02",
        "",
        f"Protocolo: {protocol_sha}. Fonte PDF: {SOURCE_SHA}.",
        "",
        (
            "Pesos ajustados com exemplos histÃ³ricos; nenhuma ordem ou ativaÃ§Ã£o. "
            "ValidaÃ§Ã£o 2024/2025 Ã© desenvolvimento reutilizado, nÃ£o teste independente. "
            "2026 nÃ£o usado nesta rodada."
        ),
        "",
        (
            "| Especialista/candidato | OperaÃ§Ãµes validaÃ§Ã£o | MÃ©dia R lÃ\xadquida | PF | "
            "Stress mÃ©dia R | Gate desenvolvimento |"
        ),
        "|---|---:|---:|---:|---:|---|",
    ]
    for c in results:
        lines.append(
            f"| {c['candidate']} | {c['pooled']['trades']} | "
            f"{c['pooled']['mean_r']:.4f} | {c['pooled']['profit_factor']:.3f} | "
            f"{c['stressed']['mean_r']:.4f} | {c['passed']} |"
        )
    lines += [
        "",
        (
            "OTE local de12 barras Ã© caracterÃ\xadstica aproximada; sequÃªncia CRT/OTE completa "
            "Ã© outra caracterÃ\xadstica separada. H4 anterior completo e padrÃµes sÃ£o causais. "
            "NÃ£o equivale a ensinar todas as regras discricionÃ¡rias do PDF."
        ),
        "",
        (
            "RÃ³tulos usam custos e barreiras; contagem de exemplos nÃ£o Ã© contagem de "
            "operaÃ§Ãµes executadas. Backtest reaproveita conversÃµes cambiais fixas do "
            "protocolo para risco/custos. Carteira conjunta, kill/drawdown dinÃ¢mico e "
            "reconciliaÃ§Ã£o nÃ£o foram revalidados aqui; nÃ£o hÃ¡ permissÃ£o de execuÃ§Ã£o "
            "decorrente deste treino."
        ),
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, default=str), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path("data/fx/ibkr-m5"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--source", type=Path, default=Path("C:/Users/guilo/Downloads/Aula 02 - semana vip.pdf")
    )
    a = p.parse_args()
    run(a.data, a.out, a.source)
