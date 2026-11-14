"""Frozen magnitude-weighted TRAIN experiment. No broker or activation API."""

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

VERSION = "m5-magnitude-training/1.0.0"
MIN_MAGNITUDE_R = 0.25
MAX_MAGNITUDE_R = 3.0


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


def magnitude_weights(
    index: pd.DatetimeIndex, target: pd.DataFrame, eligible: np.ndarray, weights: np.ndarray
) -> np.ndarray:
    """TRAIN-only supervised magnitude emphasis; never an input feature."""
    if not target.index.equals(index) or len(eligible) != len(index):
        raise ValueError("aligned target/index/mask required")
    values = target.net_r.to_numpy(float)[eligible]
    weights = np.asarray(weights, dtype=float)
    if not len(values) or len(values) != len(weights):
        raise ValueError("nonempty aligned purged TRAIN targets required")
    if not np.isfinite(values).all() or not np.isfinite(weights).all() or (weights <= 0).any():
        raise ValueError("finite TRAIN targets and positive specialty weights required")
    magnitude = np.clip(np.abs(values), MIN_MAGNITUDE_R, MAX_MAGNITUDE_R)
    return np.asarray(weights * magnitude / magnitude.mean(), dtype=float)


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
            "Emphasize magnitude of purged TRAIN supervised outcomes: abs(netR) clipped "
            "to fixed0.25..3R, normalized mean1 per pair, multiplied by specialty5/1. "
            "No recency/uniqueness combination. Features, labels, gates and costs unchanged. "
            "For regressors this changes the estimand away from ordinary expected netR; "
            "it is a tail-emphasis hypothesis, NOT direct expected-profit optimization. "
            "Classifier scores are magnitude-weighted, not calibrated win probabilities."
        ),
        min_magnitude_r=MIN_MAGNITUDE_R,
        max_magnitude_r=MAX_MAGNITUDE_R,
        daily_diagnostics_sha256=hashlib.sha256(Path(daily.__file__).read_bytes()).hexdigest(),
        desired_qualified_specialists=2,
        user_objective="Net end-of-day profitability after all costs, not 100% accuracy.",
        acceptance="Original frozen gates remain mandatory; daily net diagnostics are additional.",
        stopping_rule=(
            "Six fixed depth3/5 candidates once. No adaptive magnitude bounds or gate changes."
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
        sample_weight_function=magnitude_weights,
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
