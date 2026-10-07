"""Preprocessing fitted on TRAIN rows only, then frozen.

fit(X_train):
  - for each OPTIONAL feature: a missing-indicator column `<f>__missing` (1 if NaN) and the TRAIN
    median as the fill value (median of the finite TRAIN values; 0.0 if a feature is never
    observed in TRAIN, which is then constant and carries no information);
  - mean / std (ddof 0) of every resulting column on TRAIN; std == 0 -> scale 1.
transform(X): applies exactly the frozen parameters. A NaN in a REQUIRED feature raises (such a
row is not `valid` and must never reach the model).
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.ml.features import FeatureSpec


@dataclass(frozen=True)
class FittedPreprocessor:
    features: tuple[str, ...]
    optional: tuple[str, ...]
    fill_values: tuple[float, ...]
    output_columns: tuple[str, ...]
    means: np.ndarray
    scales: np.ndarray
    fitted_rows: int

    def _expand(self, X: pd.DataFrame) -> pd.DataFrame:  # noqa: N803
        if list(X.columns) != list(self.features):
            raise ValueError("feature columns differ from the fitted preprocessor")
        required = [f for f in self.features if f not in self.optional]
        if len(X) and not np.isfinite(X[required].to_numpy(dtype=np.float64)).all():
            raise ValueError("required feature is undefined; row must not be scored")
        out = X.astype("float64").copy()
        for name, fill in zip(self.optional, self.fill_values, strict=True):
            out[f"{name}__missing"] = out[name].isna().astype("float64")
            out[name] = out[name].fillna(fill)
        return out[list(self.output_columns)]

    def transform(self, X: pd.DataFrame) -> np.ndarray:  # noqa: N803
        scaled: np.ndarray = (self._expand(X).to_numpy(dtype=np.float64) - self.means) / self.scales
        return scaled


def fit_preprocessor(X_train: pd.DataFrame, specs: tuple[FeatureSpec, ...]) -> FittedPreprocessor:  # noqa: N803
    if X_train.empty:
        raise ValueError("cannot fit preprocessing on an empty training set")
    optional = tuple(s.name for s in specs if s.optional)
    fills = []
    for name in optional:
        finite = X_train[name].to_numpy(dtype=np.float64)
        finite = finite[np.isfinite(finite)]
        fills.append(float(np.median(finite)) if finite.size else 0.0)
    features = tuple(X_train.columns)
    output = (*features, *(f"{n}__missing" for n in optional))
    partial = FittedPreprocessor(
        features,
        optional,
        tuple(fills),
        output,
        np.zeros(len(output)),
        np.ones(len(output)),
        len(X_train),
    )
    expanded = partial._expand(X_train).to_numpy(dtype=np.float64)
    means = expanded.mean(axis=0)
    std = expanded.std(axis=0, ddof=0)
    scales = np.where(std > 0, std, 1.0)
    return FittedPreprocessor(features, optional, tuple(fills), output, means, scales, len(X_train))
