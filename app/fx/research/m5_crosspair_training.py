"""Offline market-transfer research: fit two pairs, evaluate the excluded third pair."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.fx.agents.trained import FEATURE_NAMES, feature_frame
from app.fx.research import m5_boosted_training as boosted
from app.fx.research import m5_lesson_training as lesson
from app.fx.research.m5_validation import (
    PROTOCOL,
    ROLES,
    check_manifest,
    gate,
    load_m5,
    mid_of,
    pattern_frame,
    save_trades,
    simulate,
    write_json,
)

VERSION = "m5-market-transfer/1.0.0"


def sample_excluding(
    target: str,
    before: pd.Timestamp,
    frames: dict[str, pd.DataFrame],
    xs: dict[str, np.ndarray],
    ys: dict[str, pd.DataFrame],
    context: dict[str, pd.DataFrame],
    masks: dict[str, np.ndarray],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    if target not in frames or len(frames) < 3:
        raise ValueError("three markets with a known excluded target required")
    training = sorted(s for s in frames if s != target)
    eligible = {
        s: lesson.purged_mask(pd.DatetimeIndex(frames[s].index), ys[s], before)
        & np.isfinite(xs[s]).all(axis=1)
        & context[s].h4_available.to_numpy(bool)
        & (context[s].h4_bias.to_numpy(float) < 0)
        for s in training
    }
    x = np.concatenate([xs[s][eligible[s]] for s in training])
    y = np.concatenate([ys[s].net_r.to_numpy(float)[eligible[s]] for s in training])
    weights = np.concatenate([np.where(masks[s][eligible[s]], 5.0, 1.0) for s in training])
    if len(y) < 300:
        raise ValueError("insufficient purged cross-market training rows")
    return x, y, weights, training


def frozen_protocol(source: Path) -> dict[str, Any]:
    p = boosted.protocol(source)
    p.update(
        schema=VERSION,
        model_trainer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        training_source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        boosted_library_sha256=hashlib.sha256(Path(boosted.__file__).read_bytes()).hexdigest(),
        curriculum_library_sha256=hashlib.sha256(Path(lesson.__file__).read_bytes()).hexdigest(),
        hypothesis=(
            "Pair-specific overfit is tested by excluding the target pair from each "
            "fit. Same causal Aula/candle features, costs, thresholds and acceptance "
            "gate. Each specialist is a fixed three-route model, one "
            "training-on-the-other-pairs predictor per target pair."
        ),
        selection=(
            "Exactly six declared role/depth candidates. Pool all three routed market "
            "signals into one globally limited simulation per candidate and year; no "
            "best-pair cherry-picking."
        ),
        evaluation_limit=(
            "2024/2025 are reused development years, not a fresh statistical test. "
            "Pair is excluded from that fit but was seen in earlier research. "
            "Holdout2026 remains unused in this experiment."
        ),
        deployment=(
            "No paper activation. A passing candidate still needs frozen selection, "
            "temporal holdout, joint risk/cash/protected execution integration of the "
            "routed architecture."
        ),
    )
    return p


def run(data: Path, out: Path, source: Path) -> None:
    if out.exists():
        raise ValueError("frozen output exists; never overwrite or retry blindly")
    protocol = frozen_protocol(source)
    out.mkdir(parents=True)
    sha = write_json(out / "protocol.json", protocol)
    for name, src in [
        ("training_source.py", Path(__file__)),
        ("boosted_library.py", Path(boosted.__file__)),
        ("curriculum_library.py", Path(lesson.__file__)),
    ]:
        (out / name).write_bytes(src.read_bytes())
    print(f"Frozen cross-market protocol {sha}", flush=True)
    write_json(out / "data_manifest.json", check_manifest(data))
    frames = {s: load_m5(data, s, pd.Timestamp(PROTOCOL.holdout_start)) for s in PROTOCOL.pairs}
    fs = {}
    patterns = {}
    context = {}
    xs = {}
    ys = {}
    results = []
    for symbol, df in frames.items():
        mid = mid_of(df)
        fs[symbol] = feature_frame(mid, "M5")
        patterns[symbol] = pattern_frame(mid)
        context[symbol] = lesson.lesson_features(mid, patterns[symbol])
        xs[symbol] = pd.concat([fs[symbol][list(FEATURE_NAMES)], context[symbol]], axis=1).to_numpy(
            float
        )
        ys[symbol] = lesson.labels(df, fs[symbol], symbol)
        print(f"Prepared {symbol}: {len(df)} native bars", flush=True)
    for role in ROLES:
        masks = {s: lesson.research_mask(fs[s], patterns[s], context[s], role) for s in frames}
        for depth in (3.0, 5.0):
            candidate = f"{role}-{depth:g}"
            periods = {}
            stressed = []
            folds = []
            for year, lo, hi in PROTOCOL.folds:
                before = pd.Timestamp(lo)
                signal_frames = {}
                details = []
                for target in PROTOCOL.pairs:
                    x, y, w, training = sample_excluding(
                        target, before, frames, xs, ys, context, masks
                    )
                    fit, artifact = boosted.fitted_model(role, depth, x, y, sample_weight=w)
                    artifact.update(
                        schema=VERSION,
                        protocol_sha256=sha,
                        training_end=lo,
                        excluded_target_pair=target,
                        training_pairs=training,
                        specialty_weighted_examples=int((w > 1).sum()),
                    )
                    folder = out / "models"
                    folder.mkdir(exist_ok=True)
                    name = f"{candidate}-{year}-{target}.json"
                    model_sha = write_json(folder / name, artifact)
                    forecast = boosted.score(fit, np.nan_to_num(xs[target]), role)
                    signal_frames[target] = lesson.signals(
                        frames[target], fs[target], masks[target], forecast, role, target
                    )
                    details.append(
                        {
                            "excluded_target": target,
                            "training_pairs": training,
                            "rows": len(y),
                            "model_file": name,
                            "model_sha256": model_sha,
                        }
                    )
                    print(f"Trained {candidate} {year}: target={target}, rows={len(y)}", flush=True)
                periods[year] = simulate(
                    frames, signal_frames, before, pd.Timestamp(hi), PROTOCOL.base_costs
                )
                stress = simulate(
                    frames, signal_frames, before, pd.Timestamp(hi), PROTOCOL.stress_costs
                )
                stressed.append(stress)
                folds.append(
                    {
                        "year": year,
                        "routes": details,
                        "base_trades_sha256": save_trades(
                            out / "trades", candidate + "-" + year + "-base", periods[year]
                        ),
                        "stress_trades_sha256": save_trades(
                            out / "trades", candidate + "-" + year + "-stress", stress
                        ),
                    }
                )
                print(
                    f"Evaluated {candidate} {year}: {len(periods[year])} trades "
                    "with shared5/day3positions",
                    flush=True,
                )
            result = gate(periods, pd.concat(stressed, ignore_index=True))
            results.append(
                {"candidate": candidate, "role": role, "depth": depth, "folds": folds, **result}
            )
            write_json(out / "candidates.json", results)
            print(f"{candidate} development_gate={result['passed']}", flush=True)
    final_models = []
    for role in ROLES:
        chosen = max((c for c in results if c["role"] == role), key=lambda c: c["pooled"]["mean_r"])
        masks = {s: lesson.research_mask(fs[s], patterns[s], context[s], role) for s in frames}
        for target in PROTOCOL.pairs:
            x, y, w, training = sample_excluding(
                target, pd.Timestamp(PROTOCOL.holdout_start), frames, xs, ys, context, masks
            )
            _, artifact = boosted.fitted_model(role, chosen["depth"], x, y, sample_weight=w)
            artifact.update(
                schema=VERSION,
                protocol_sha256=sha,
                training_end=PROTOCOL.holdout_start,
                excluded_target_pair=target,
                training_pairs=training,
                development_passed=chosen["passed"],
                requires_independent_and_joint_validation=True,
            )
            name = f"{role}-{target}-development-final.json"
            write_json(out / "models" / name, artifact)
            final_models.append(name)
    summary = {
        "completed_at": datetime.now(UTC).isoformat(),
        "protocol_sha256": sha,
        "models_trained": len(list((out / "models").glob("*.json"))),
        "specialists": list(ROLES),
        "passed_development_candidates": sum(c["passed"] for c in results),
        "holdout_assessed": False,
        "paper_activated": False,
        "historically_approved": False,
        "orders_sent": 0,
        "final_model_files": final_models,
        "cross_market_training": True,
    }
    write_json(out / "summary.json", summary)
    lines = [
        "# Treino M5 com transferencia entre pares",
        "",
        f"Protocolo: {sha}.",
        "",
        (
            "Cada modelo exclui do treino o par em que sera avaliado; simulacao "
            "agrega os3pares com5entradas/dia e3posicoes. Os anos2024/25 ja foram "
            "vistos em pesquisa; nao sao teste independente.2026 nao utilizado."
        ),
        "",
        "| Candidato | Trades | Media liquida R | PF | Stress media R | Passou |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for c in results:
        lines.append(
            f"| {c['candidate']} | {c['pooled']['trades']} | "
            f"{c['pooled']['mean_r']:.4f} | {c['pooled']['profit_factor']:.4f} | "
            f"{c['stressed']['mean_r']:.4f} | {c['passed']} |"
        )
    lines += [
        "",
        (
            "Pesos e modelos finais sao apenas de estudo. Mantidos custos, "
            "purga/embargo, horizonte96 e criterios de aceite. Nenhuma mudanca em "
            "ordens/limites/H1; carteira conjunta dos tres especialistas e pipeline "
            "protegido ainda exigem validacao antes de habilitar qualquer execucao."
        ),
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path("data/fx/ibkr-m5"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--source", type=Path, default=Path("C:/Users/guilo/Downloads/Aula 02 - semana vip.pdf")
    )
    a = p.parse_args()
    run(a.data, a.out, a.source)
