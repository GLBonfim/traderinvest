"""Training on TRAIN rows only and prediction for every valid row.

Training rows = availability `valid` AND target defined AND period `train` (purged rows
excluded). The preprocessor and the model are fitted on exactly those rows; nothing from
VALIDATION or TEST is visible during fitting (enforced by `assert_chronological`).
"""

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.ml.config import MLConfig
from app.ml.features import VALID, FeatureDataset
from app.ml.models import build_model
from app.ml.preprocessing import FittedPreprocessor, fit_preprocessor
from app.ml.split import assert_chronological
from app.strategies.base import FLAT, INSUFFICIENT_DATA, LONG


@dataclass(frozen=True)
class TrainedModel:
    name: str
    estimator: Any
    preprocessor: FittedPreprocessor
    train_index: pd.DatetimeIndex
    seed: int


def scoreable(dataset: FeatureDataset) -> pd.Series:
    return dataset.frame["availability"] == VALID


def training_mask(dataset: FeatureDataset, target: pd.Series, periods: pd.Series) -> pd.Series:
    return scoreable(dataset) & target.notna() & (periods == "train")


def train_model(
    name: str, cfg: MLConfig, dataset: FeatureDataset, target: pd.Series, periods: pd.Series
) -> TrainedModel:
    mask = training_mask(dataset, target, periods)
    train_idx = pd.DatetimeIndex(dataset.frame.index[mask])
    later = pd.DatetimeIndex(dataset.frame.index[periods.isin(["validation", "test"])])
    assert_chronological(train_idx, later)
    X_train = dataset.X[mask]  # noqa: N806
    y = target[mask].to_numpy(dtype=np.int64)
    if len(np.unique(y)) < 2:
        raise ValueError("training target has a single class")
    pre = fit_preprocessor(X_train, dataset.specs)
    est = build_model(name, cfg)
    est.fit(pre.transform(X_train), y)
    return TrainedModel(name, est, pre, train_idx, cfg.seed)


def predict_proba(model: TrainedModel, dataset: FeatureDataset) -> pd.Series:
    """P(target = 1) for every valid row (including the last row without a target); NaN else."""
    ok = scoreable(dataset)
    out = pd.Series(np.nan, index=dataset.frame.index, name=model.name)
    if ok.any():
        X = model.preprocessor.transform(dataset.X[ok])  # noqa: N806
        out[ok] = model.estimator.predict_proba(X)[:, 1]
    return out


def states_from_probabilities(p: pd.Series, threshold: float) -> pd.Series:
    """Deterministic rule: P(up) > threshold -> LONG; <= threshold -> FLAT; undefined ->
    INSUFFICIENT_DATA (never FLAT)."""
    return pd.Series(
        np.select([p.isna(), p > threshold], [INSUFFICIENT_DATA, LONG], FLAT),
        index=p.index,
        dtype=object,
    )
