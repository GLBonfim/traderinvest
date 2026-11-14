"""Frozen specialty-balanced TRAIN experiment. No broker or activation API."""

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

VERSION = "m5-specialty-balanced/1.0.0"


def frozen_sources() -> dict[str, Path]:
    repo = Path(__file__).resolve().parents[3]
    return {
        "model_trainer.py": Path(__file__),
        "boosted_library.py": Path(boosted.__file__),
        "economic_library.py": Path(economic.__file__),
        "daily_diagnostics.py": Path(daily.__file__),
        "validation_library.py": repo / "app/fx/research/m5_validation.py",
        "backtest_library.py": repo / "app/fx/research/backtest.py",
        "trained_features.py": repo / "app/fx/agents/trained.py",
        "lesson_m5_features.py": repo / "app/candles/lesson_m5.py",
        "curriculum_library.py": Path(lesson.__file__),
    }


def balanced_weights(
    index: pd.DatetimeIndex, target: pd.DataFrame, eligible: np.ndarray, weights: np.ndarray
) -> np.ndarray:
    """Balance TRAIN setup/context mass without reading target outcomes."""
    weights = np.asarray(weights, dtype=float)
    if len(eligible) != len(index) or int(np.count_nonzero(eligible)) != len(weights):
        raise ValueError("aligned purged TRAIN mask and weights required")
    if not len(weights) or not np.isin(weights, [1.0, 5.0]).all():
        raise ValueError("original specialty5/1 weights required")
    matching = weights == 5.0
    n_matching, n_background = int(matching.sum()), int((~matching).sum())
    if not n_matching or not n_background:
        raise ValueError("both TRAIN specialty and background groups required")
    mass = weights.sum()
    return np.asarray(
        np.where(matching, mass / (2 * n_matching), mass / (2 * n_background)), dtype=float
    )


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
        frozen_source_sha256={
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in frozen_sources().items()
        },
        evaluator_comparability=(
            "Current audited evaluator differs from earlier runs; this is a new "
            "exploratory run, not an isolated causal comparison to old results."
        ),
        model_trainer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        economic_library_sha256=hashlib.sha256(Path(economic.__file__).read_bytes()).hexdigest(),
        hypothesis=(
            "Original specialty5/1 mass is dominated by background. Redistribute each "
            "pair purged TRAIN total weight W equally across matching setup/background: "
            "W/(2*nmatching) and W/(2*nbackground). Preserve W and pair contribution. "
            "No outcome magnitude, recency or uniqueness weights. Same gates, labels, "
            "thresholds, features and costs. Empty groups refuse. Rare setups risk "
            "overfit; weights create no new observations. Weighted classifier scores "
            "are not calibrated win probabilities."
        ),
        specialty_mass_fraction=0.5,
        training_total_weight="Preserved original sum of specialty5/1 weights per pair.",
        daily_diagnostics_sha256=hashlib.sha256(Path(daily.__file__).read_bytes()).hexdigest(),
        desired_qualified_specialists=2,
        user_objective="Net end-of-day profitability after all costs, not 100% accuracy.",
        acceptance="Original frozen gates remain mandatory; daily net diagnostics are additional.",
        stopping_rule=(
            "Six fixed depth3/5 candidates once. No adaptive balancing fractions or gate changes."
        ),
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
        sample_weight_function=balanced_weights,
        source_files=frozen_sources(),
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
