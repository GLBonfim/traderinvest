"""The two pre-declared model families (no hyperparameter search).

1. logistic_regression — linear baseline (L2, C = 1, lbfgs, no class weighting).
2. random_forest — one simple non-linear model (300 shallow trees, depth 4, >= 50 samples per
   leaf, sqrt features, single-threaded), justified as the minimal test of whether
   non-linear interactions of the same features add anything.
Both are seeded; with n_jobs = 1 their output is bit-reproducible.
"""

from typing import Any

from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression

from app.ml.config import MLConfig


def build_model(name: str, cfg: MLConfig) -> Any:
    if name == "logistic_regression":
        lr = cfg.logistic
        return LogisticRegression(
            C=lr.C,
            solver=lr.solver,
            max_iter=lr.max_iter,
            class_weight=lr.class_weight,
            random_state=cfg.seed,
        )
    if name == "random_forest":
        rf = cfg.forest
        return RandomForestClassifier(
            n_estimators=rf.n_estimators,
            max_depth=rf.max_depth,
            min_samples_leaf=rf.min_samples_leaf,
            max_features=rf.max_features,
            class_weight=rf.class_weight,
            n_jobs=rf.n_jobs,
            random_state=cfg.seed,
        )
    raise ValueError(f"unknown model {name!r}")
