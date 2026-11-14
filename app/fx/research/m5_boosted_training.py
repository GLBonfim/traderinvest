"""Predeclared non-linear M5 research round. Offline; no broker or activation API."""

import argparse
import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from threadpoolctl import threadpool_limits  # type: ignore[import-untyped]

from app.fx.research import m5_lesson_training as curriculum
from app.fx.research.m5_validation import ROLES

VERSION = "m5-aula-boosted/2.0.0"


def portable_predict(artifact: dict[str, Any], x: np.ndarray) -> np.ndarray:
    names = artifact["feature_names"]
    if x.ndim != 2 or x.shape[1] != len(names) or not np.isfinite(x).all():
        raise ValueError("portable predictor requires finite versioned feature matrix")
    z = (x - np.asarray(artifact["center"])) / np.asarray(artifact["scale"])
    result = np.full(len(x), float(artifact["baseline"]))
    rows = np.arange(len(x))
    for tree in artifact["boosted_trees"]:
        node = np.zeros(len(x), dtype=int)
        leaf = np.asarray(tree["is_leaf"], dtype=bool)
        left, right = np.asarray(tree["left"]), np.asarray(tree["right"])
        feature = np.asarray(tree["feature_idx"])
        threshold = np.asarray(tree["num_threshold"])
        for _ in range(artifact["max_depth"] + 1):
            active = ~leaf[node]
            if not active.any():
                break
            current = node[active]
            go_left = z[rows[active], feature[current]] <= threshold[current]
            node[active] = np.where(go_left, left[current], right[current])
        if not leaf[node].all():
            raise ValueError("tree depth/export invalid")
        result += np.asarray(tree["value"])[node]
    return 1 / (1 + np.exp(-np.clip(result, -700, 700))) if artifact["classifier"] else result


def fitted_model(
    role: str,
    variant: float,
    x: np.ndarray,
    y: np.ndarray,
    sample_weight: np.ndarray | None = None,
    *,
    feature_names: tuple[str, ...] | None = None,
) -> tuple[Any, dict[str, Any]]:
    names = curriculum.NAMES if feature_names is None else feature_names
    if x.ndim != 2 or x.shape[1] != len(names):
        raise ValueError("feature dimensions/version differ")
    if variant not in (3, 5):
        raise ValueError("only the two frozen depths allowed")
    center, scale = x.mean(axis=0), x.std(axis=0)
    scale = np.where(scale > 1e-8, scale, 1.0)
    z = (x - center) / scale
    classifier = role == "impulso"
    cls = HistGradientBoostingClassifier if classifier else HistGradientBoostingRegressor
    model = cls(
        max_iter=160,
        learning_rate=0.05,
        max_depth=int(variant),
        max_leaf_nodes=2 ** int(variant),
        min_samples_leaf=100,
        l2_regularization=10.0,
        early_stopping=False,
        random_state=7,
    )
    with threadpool_limits(limits=2):
        model.fit(z, (y > 0).astype(int) if classifier else y, sample_weight=sample_weight)
    trees = []
    for stage in model._predictors:
        nodes = stage[0].nodes
        trees.append(
            {
                key: nodes[key].tolist()
                for key in ("value", "feature_idx", "num_threshold", "left", "right", "is_leaf")
            }
        )
    artifact = {
        "schema": VERSION,
        "algorithm": type(model).__name__,
        "sklearn_version": sklearn.__version__,
        "feature_names": names,
        "role": role,
        "variant": variant,
        "center": center.tolist(),
        "scale": scale.tolist(),
        "classifier": classifier,
        "baseline": float(model._baseline_prediction[0, 0]),
        "boosted_trees": trees,
        "max_depth": int(variant),
        "training_rows": len(y),
        "training_positive_labels": int((y > 0).sum()),
        "offline_only": True,
        "execution_enabled": False,
        "statistically_approved": False,
    }
    sample = np.arange(0, len(x), max(1, len(x) // 200))[:200]
    with threadpool_limits(limits=2):
        original = model.predict_proba(z[sample])[:, 1] if classifier else model.predict(z[sample])
    exported = portable_predict(artifact, x[sample])
    if not np.allclose(original, exported, rtol=1e-10, atol=1e-10):
        raise ValueError("portable export prediction mismatch")
    artifact["export_max_abs_error"] = float(np.max(np.abs(original - exported)))
    return artifact, artifact


def score(fit: Any, x: np.ndarray, role: str) -> np.ndarray:
    return portable_predict(fit, x)


def protocol(source: Path) -> dict[str, Any]:
    p = curriculum.frozen_protocol(source)
    p.update(
        schema=VERSION,
        variants={r: [3.0, 5.0] for r in ROLES},
        model_trainer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        hypothesis=(
            "Nonlinear interactions of the same causal Aula/candle features; no threshold "
            "or gate changes; fixed two depth candidates per specialist. No adaptive "
            "search after results."
        ),
        estimator={
            "algorithm": "histogram gradient boosting",
            "iterations": 160,
            "learning_rate": 0.05,
            "min_samples_leaf": 100,
            "l2": 10,
            "early_stopping": False,
            "threads": 2,
            "seed": 7,
        },
        stopping_rule=(
            "After six declared candidates stop this search. Never keep changing "
            "parameters until a historical pass. Failed runs stay recorded; only a "
            "predeclared fresh experiment can follow."
        ),
        activation_policy=(
            "Offline-only; selection/future holdout and joint/live parity still required. "
            "Never directly install trained weights."
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
    curriculum.run(
        a.data,
        a.out,
        a.source,
        fit_function=fitted_model,
        score_function=score,
        protocol_function=protocol,
    )
    (a.out / "model_trainer.py").write_bytes(Path(__file__).read_bytes())
