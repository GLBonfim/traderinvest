"""Classification metrics and the block-bootstrap ROC-AUC interval."""

import numpy as np
from sklearn.metrics import (
    accuracy_score,
    balanced_accuracy_score,
    confusion_matrix,
    f1_score,
    precision_score,
    recall_score,
    roc_auc_score,
)

from app.validation.resampling import moving_block_indices, percentile_interval

NAN = float("nan")


def classification_metrics(y: np.ndarray, p: np.ndarray, threshold: float) -> dict[str, float]:
    """Metrics on rows with a defined target and prediction. ROC-AUC is NaN with one class."""
    if y.size == 0:
        return {"n": 0.0}
    pred = (p > threshold).astype(np.int64)
    tn, fp, fn, tp = confusion_matrix(y, pred, labels=[0, 1]).ravel()
    both = len(np.unique(y)) == 2
    return {
        "n": float(y.size),
        "positive_rate": float(y.mean()),
        "predicted_positive_rate": float(pred.mean()),
        "accuracy": float(accuracy_score(y, pred)),
        "balanced_accuracy": float(balanced_accuracy_score(y, pred)) if both else NAN,
        "precision": float(precision_score(y, pred, zero_division=np.nan)),
        "recall": float(recall_score(y, pred, zero_division=np.nan)),
        "f1": float(f1_score(y, pred, zero_division=np.nan)),
        "roc_auc": float(roc_auc_score(y, p)) if both else NAN,
        "tn": float(tn),
        "fp": float(fp),
        "fn": float(fn),
        "tp": float(tp),
        "always_up_accuracy": float(y.mean()),  # naive reference classifier
    }


def auc_interval(
    y: np.ndarray,
    p: np.ndarray,
    *,
    block_length: int,
    n_resamples: int,
    seed: int,
    confidence: float,
) -> tuple[float, float, float, int]:
    """(point, lower, upper, valid resamples): moving-block bootstrap over the time-ordered
    evaluation rows (same Phase 9 resampler); resamples with a single class are skipped."""
    point = float(roc_auc_score(y, p)) if len(np.unique(y)) == 2 else NAN
    idx = moving_block_indices(len(y), block_length, n_resamples, seed)
    samples = np.full(n_resamples, np.nan)
    for b in range(n_resamples):
        yb = y[idx[b]]
        if len(np.unique(yb)) == 2:
            samples[b] = roc_auc_score(yb, p[idx[b]])
    lo, hi, valid = percentile_interval(samples, confidence)
    return point, lo, hi, valid
