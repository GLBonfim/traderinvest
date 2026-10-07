"""Chronological TRAIN / VALIDATION / TEST assignment. No random split exists in this module.

Rows are assigned by bar date. The last `purge_sessions` rows of TRAIN and of VALIDATION are
re-labelled "purged" (their target uses a session of the next period). Rows before the first
valid feature row or with no target are handled by availability, not by the split.
"""

import numpy as np
import pandas as pd

from app.ml.config import SplitSpec

PERIODS = ("train", "validation", "test")


def assign_periods(index: pd.DatetimeIndex, spec: SplitSpec) -> pd.Series:
    if not index.is_monotonic_increasing or not index.is_unique:
        raise ValueError("rows must be in strictly increasing time order (no shuffling)")
    d = pd.Series(index.date, index=index)
    period = pd.Series("outside", index=index, dtype=object)
    train = d <= spec.train_end
    if spec.train_start:
        train &= d >= spec.train_start
    period[train] = "train"
    period[(d >= spec.validation_start) & (d <= spec.validation_end)] = "validation"
    test = d >= spec.test_start
    if spec.test_end:
        test &= d <= spec.test_end
    period[test] = "test"
    for name in ("train", "validation"):
        pos = np.flatnonzero((period == name).to_numpy())
        if pos.size:
            period.iloc[pos[-spec.purge_sessions :]] = "purged"
    return period


def assert_chronological(train: pd.DatetimeIndex, later: pd.DatetimeIndex) -> None:
    """Rejects any split where a training row is not strictly before every later row."""
    if len(train) and len(later) and train.max() >= later.min():
        raise ValueError(
            "training rows must all precede evaluation rows (random/overlapping split)"
        )
