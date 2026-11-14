"""Offline event-uniqueness experiment; frozen economic features and original gates."""

import argparse
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.fx.research import m5_boosted_training as boosted
from app.fx.research import m5_economic_training as economic
from app.fx.research import m5_lesson_training as lesson

VERSION = "m5-event-uniqueness/1.0.0"


def unique_weights(
    index: pd.DatetimeIndex, target: pd.DataFrame, eligible: np.ndarray, weights: np.ndarray
) -> np.ndarray:
    """Mean inverse concurrency over full known horizons, using TRAIN events only.

    Outcome values are not read. Fixed horizon endpoints alone determine overlap.
    Normalize uniqueness to mean one before multiplying unchanged specialty weights.
    """
    starts = np.flatnonzero(eligible)
    if not len(starts) or len(weights) != len(starts):
        raise ValueError("nonempty aligned train events required")
    ends = index.searchsorted(pd.DatetimeIndex(target.label_end.iloc[starts]), side="left")
    if (ends <= starts).any() or (ends > len(index)).any():
        raise ValueError("invalid training horizon")
    counts = np.zeros(len(index) + 1, dtype=float)
    np.add.at(counts, starts, 1)
    np.add.at(counts, ends, -1)
    concurrent = np.cumsum(counts[:-1])
    inverse = np.divide(1.0, concurrent, out=np.zeros_like(concurrent), where=concurrent > 0)
    prefix = np.concatenate(([0.0], np.cumsum(inverse)))
    uniqueness = (prefix[ends] - prefix[starts]) / (ends - starts)
    if not np.isfinite(uniqueness).all() or (uniqueness <= 0).any():
        raise ValueError("invalid uniqueness")
    return np.asarray(weights * (uniqueness / uniqueness.mean()), dtype=float)


def protocol(source: Path) -> dict[str, Any]:
    p = economic.protocol(source)
    p.update(
        schema=VERSION,
        model_trainer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        economic_library_sha256=hashlib.sha256(Path(economic.__file__).read_bytes()).hexdigest(),
        hypothesis=(
            "Overlapping 96-bar labels overrepresent the same price movement. "
            "Weight each purged TRAIN event by mean inverse concurrency, normalized "
            "to mean one per pair, then multiply unchanged specialty weight5/1. "
            "All six depths,51features,labels,thresholds,costs and gates unchanged."
        ),
        uniqueness="Per pair, TRAIN eligible events only; half-open [signal,full_label_end).",
        stopping_rule="One frozen six-candidate experiment; no repeated fitting until pass.",
        desired_qualified_specialists=2,
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
        fit_function=economic.fit,
        score_function=boosted.score,
        protocol_function=protocol,
        feature_function=economic.augment,
        sample_weight_function=unique_weights,
        source_files={
            "model_trainer.py": Path(__file__),
            "boosted_library.py": Path(boosted.__file__),
            "economic_library.py": Path(economic.__file__),
        },
    )
