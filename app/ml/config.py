"""Phase 10 configuration. Every value is declared BEFORE any model result is seen (ADR-0019).

Split dates, target, feature set, model hyperparameters, decision threshold, seed and the
incremental-edge decision rule are fixed here. Nothing may be tuned on test results.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date
from itertools import pairwise

ML_VERSION = "1.0.0"
FEATURE_VERSION = "1.0.0"


@dataclass(frozen=True)
class SplitSpec:
    """Chronological TRAIN / VALIDATION / TEST periods (inclusive dates).

    Rationale (fixed in advance): TRAIN ends 2009-12-31, the boundary already used by the Phase 9
    `early` slice; the remaining ~16.75 years are split at 2018-01-01 into VALIDATION
    (2010-2017, 8 years) and TEST (2018 onwards, ~8.75 years incl. 2020 and 2022).
    `purge_sessions` drops the last rows of TRAIN and VALIDATION whose target would use a
    session of the next period (target horizon = 1 session).
    """

    train_start: date | None = None
    train_end: date = date(2009, 12, 31)
    validation_start: date = date(2010, 1, 1)
    validation_end: date = date(2017, 12, 31)
    test_start: date = date(2018, 1, 1)
    test_end: date | None = None
    purge_sessions: int = 1

    def __post_init__(self) -> None:
        ordered = [self.train_end, self.validation_start, self.validation_end, self.test_start]
        if self.train_start and self.train_start > self.train_end:
            raise ValueError("train_start > train_end")
        if not all(a < b for a, b in pairwise(ordered)):
            raise ValueError("periods must be chronological and non-overlapping")
        if self.test_end and self.test_end < self.test_start:
            raise ValueError("test_end < test_start")
        if self.purge_sessions < 1:
            raise ValueError("purge_sessions must be >= the target horizon (1)")


@dataclass(frozen=True)
class LogisticSpec:
    C: float = 1.0  # scikit-learn parameter name
    penalty: str = "l2"
    solver: str = "lbfgs"
    max_iter: int = 2000
    class_weight: str | None = None  # no automatic balancing (imbalance is reported first)


@dataclass(frozen=True)
class ForestSpec:
    n_estimators: int = 300
    max_depth: int = 4
    min_samples_leaf: int = 50
    max_features: str = "sqrt"
    class_weight: str | None = None
    n_jobs: int = 1  # single-threaded for bit-exact reproducibility


@dataclass(frozen=True)
class EdgeRule:
    """Pre-declared rule. A model shows INCREMENTAL EDGE on TEST only if ALL hold:
    1. ROC-AUC 95% block-bootstrap interval lower bound > 0.5;
    2. Sharpe difference vs the price benchmark AND vs each of the 5 timing baselines has a
       positive estimate and Holm-adjusted p < alpha (family = all models x 6 references);
    3. under the 10 bps cost scenario its net Sharpe exceeds the price benchmark's.
    Otherwise the verdict is NO INCREMENTAL EDGE FOUND."""

    alpha: float = 0.05
    auc_floor: float = 0.5
    stress_scenario: str = "C_10bps"


@dataclass(frozen=True)
class MLConfig:
    split: SplitSpec = field(default_factory=SplitSpec)
    logistic: LogisticSpec = field(default_factory=LogisticSpec)
    forest: ForestSpec = field(default_factory=ForestSpec)
    models: tuple[str, ...] = ("logistic_regression", "random_forest")
    decision_threshold: float = 0.5  # P(up) > threshold -> LONG, else FLAT (fixed, not tuned)
    seed: int = 20261007
    edge_rule: EdgeRule = field(default_factory=EdgeRule)

    def __post_init__(self) -> None:
        allowed = {"logistic_regression", "random_forest"}
        if (
            not self.models
            or not set(self.models) <= allowed
            or len(set(self.models)) != len(self.models)
        ):
            raise ValueError(f"models must be a non-empty subset of {sorted(allowed)}")
        if not 0 < self.decision_threshold < 1:
            raise ValueError("decision_threshold must be in (0, 1)")

    def fingerprint(self) -> str:
        payload = json.dumps(
            {"ml": ML_VERSION, "features": FEATURE_VERSION, **asdict(self)},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
