"""Frozen recency-weighted M5 research; net daily profitability remains unapproved."""

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.fx.research import m5_boosted_training as boosted
from app.fx.research import m5_daily_diagnostics as daily
from app.fx.research import m5_economic_training as economic
from app.fx.research import m5_lesson_training as lesson
from app.fx.research.m5_validation import write_json

VERSION = "m5-recency-training/1.0.0"
HALF_LIFE_DAYS = 180.0


def recency_weights(
    index: pd.DatetimeIndex, target: pd.DataFrame, eligible: np.ndarray, weights: np.ndarray
) -> np.ndarray:
    """Fixed calendar half-life within purged TRAIN; no validation or outcome inputs."""
    chosen = index[eligible]
    if len(chosen) == 0 or len(chosen) != len(weights):
        raise ValueError("nonempty aligned training rows required")
    age = np.asarray((chosen.max() - chosen) / pd.Timedelta(days=1), dtype=float)
    decay = np.exp2(-age / HALF_LIFE_DAYS)
    return np.asarray(weights * decay / decay.mean(), dtype=float)


def fit(
    role: str, variant: float, x: np.ndarray, y: np.ndarray, sample_weight: np.ndarray | None = None
) -> tuple[Any, dict[str, Any]]:
    model, artifact = economic.fit(role, variant, x, y, sample_weight)
    if sample_weight is not None:
        artifact["weighted_effective_rows"] = float(
            sample_weight.sum() ** 2 / np.square(sample_weight).sum()
        )
        artifact["effective_rows_note"] = "Weight concentration only, not independent trades."
    return model, artifact


def protocol(source: Path) -> dict[str, Any]:
    p = economic.protocol(source)
    p.update(
        schema=VERSION,
        model_trainer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        economic_library_sha256=hashlib.sha256(Path(economic.__file__).read_bytes()).hexdigest(),
        hypothesis=(
            "Financial regimes drift. Weight purged TRAIN examples by fixed180calendar-day "
            "half-life relative to most recent eligible training signal, normalize mean1 "
            "per pair then multiply unchanged specialty5/1. This replaces economic-v1 "
            "uniform age weights; does not combine uniqueness or tune half-life."
        ),
        half_life_days=HALF_LIFE_DAYS,
        daily_diagnostics_sha256=hashlib.sha256(Path(daily.__file__).read_bytes()).hexdigest(),
        desired_qualified_specialists=2,
        user_objective="Net end-of-day profitability after all costs, not 100% accuracy.",
        acceptance="Original frozen gates remain mandatory; daily net diagnostics are additional.",
        stopping_rule="Six fixed depth3/5 candidates once. No adaptive half-life search.",
    )
    return p


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path("data/fx/ibkr-m5"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument(
        "--source", type=Path, default=Path("C:/Users/guilo/Downloads/Aula 02 - semana vip.pdf")
    )
    a = p.parse_args()
    lesson.run(
        a.data,
        a.out,
        a.source,
        fit_function=fit,
        score_function=boosted.score,
        protocol_function=protocol,
        feature_function=economic.augment,
        sample_weight_function=recency_weights,
        source_files={
            "model_trainer.py": Path(__file__),
            "boosted_library.py": Path(boosted.__file__),
            "economic_library.py": Path(economic.__file__),
            "daily_diagnostics.py": Path(daily.__file__),
        },
    )

    candidates = json.loads((a.out / "candidates.json").read_text(encoding="utf-8"))
    result: dict[str, Any] = {}
    for candidate in candidates:
        name = candidate["candidate"]
        result[name] = {}
        for scenario in ("base", "stress"):
            files = sorted((a.out / "trades").glob(f"{name}-*-{scenario}.csv"))
            frames = [pd.read_csv(path) for path in files if path.stat().st_size > 1]
            trades = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            result[name][scenario] = daily.daily_realized(trades)
    write_json(a.out / "daily-realized-diagnostics.json", result)
