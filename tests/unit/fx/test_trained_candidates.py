"""Leakage, portable inference and experimental registry invariants."""

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.fx.agents.trained import (
    FEATURE_NAMES,
    RUN_SCHEMA,
    LinearModel,
    feature_frame,
    load_models,
    model_signals,
    retirement_decision,
)
from app.fx.research.history import resample
from app.fx.research.tournament import fit_model, mid_frame, training_rows
from tests.unit.fx.test_research_pipeline import START, history


def model() -> LinearModel:
    n = len(FEATURE_NAMES)
    return LinearModel(
        "test-ridge",
        "ridge",
        "H1",
        (0.0,) * n,
        (1.0,) * n,
        (0.0,) * n,
        1.0,
        0.35,
        "2020-01-01T00:00:00+00:00",
    )


def test_features_and_signals_never_read_future_bars() -> None:
    df = mid_frame(resample(history(days=100), "1h"))
    cutoff = 700
    changed = df.copy()
    changed.iloc[cutoff:] *= 1.25
    pd.testing.assert_frame_equal(
        feature_frame(df, "H1").iloc[:cutoff], feature_frame(changed, "H1").iloc[:cutoff]
    )
    pd.testing.assert_frame_equal(
        model_signals(df, model()).iloc[:cutoff], model_signals(df.iloc[:cutoff], model())
    )


def test_bounded_production_window_equals_research_last_signal() -> None:
    df = mid_frame(resample(history(days=100), "1h"))
    pd.testing.assert_series_equal(
        model_signals(df, model()).iloc[-1], model_signals(df.iloc[-260:], model()).iloc[-1]
    )


def test_purged_labels_and_scaler_cannot_touch_validation_data() -> None:
    df = resample(history(days=100), "1h")
    cutoff = pd.Timestamp(START) + pd.Timedelta(days=70)
    changed = df.copy()
    changed.loc[changed.index >= cutoff] *= 1.4
    x, y = training_rows({"EURUSD": df}, "H1", cutoff)
    xx, yy = training_rows({"EURUSD": changed}, "H1", cutoff)
    np.testing.assert_array_equal(x, xx)
    np.testing.assert_array_equal(y, yy)
    a = fit_model({"EURUSD": df}, "H1", "ridge", 1.0, cutoff)
    b = fit_model({"EURUSD": changed}, "H1", "ridge", 1.0, cutoff)
    assert a == b


def test_model_json_roundtrip_preserves_signals() -> None:
    m = model()
    decoded = LinearModel.from_dict(json.loads(json.dumps(m.to_dict())))
    assert decoded == m
    df = mid_frame(resample(history(days=20), "1h"))
    pd.testing.assert_frame_equal(model_signals(df, m), model_signals(df, decoded))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0.0, -1.0])
def test_invalid_scaling_is_rejected(value: float) -> None:
    with pytest.raises(ValueError):
        replace(model(), scale=(value,) * len(FEATURE_NAMES))


def test_registry_does_not_turn_research_models_into_approved_agents(tmp_path: Path) -> None:
    path = tmp_path / "models.json"
    payload = {
        "schema": RUN_SCHEMA,
        "statistically_approved": False,
        "active_model_ids": [],
        "models": [model().to_dict()],
    }
    path.write_text(json.dumps(payload))
    assert load_models(path) == []
    payload["active_model_ids"] = ["test-ridge"]
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="ineligible"):
        load_models(path)
    payload["ranking"] = [{"model_id": "test-ridge", "paper_candidate": False}]
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="ineligible"):
        load_models(path)
    payload["ranking"][0]["paper_candidate"] = True
    path.write_text(json.dumps(payload))
    assert load_models(path) == [model()]
    payload["statistically_approved"] = True
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="experimental"):
        load_models(path)


def registry() -> dict:
    return {
        "schema": RUN_SCHEMA,
        "statistically_approved": False,
        "models": [{"model_id": "current"}, {"model_id": "winner"}, {"model_id": "loser"}],
        "ranking": [
            {"model_id": "loser", "paper_candidate": False},
            {"model_id": "winner", "paper_candidate": True},
        ],
    }


def test_paper_failure_replaces_only_with_eligible_candidate() -> None:
    result = retirement_decision(registry(), "current", [-1.0] * 5, [])
    assert result["retire"] is True and result["replacement_id"] == "winner"
    assert result["drawdown_r"] == 5.0


def test_paper_falls_back_to_no_trade_after_candidates_exhausted() -> None:
    result = retirement_decision(registry(), "current", [-1.0] * 5, ["winner"])
    assert result["retire"] is True and result["replacement_id"] is None


def test_paper_observation_does_not_churn_after_few_small_losses() -> None:
    assert retirement_decision(registry(), "current", [-0.1] * 19, [])["retire"] is False
    assert retirement_decision(registry(), "current", [-0.1] * 20, [])["retire"] is True


def test_invalid_forward_returns_cannot_promote_a_replacement() -> None:
    with pytest.raises(ValueError, match="finite"):
        retirement_decision(registry(), "current", [float("nan")], [])


def test_forest_export_matches_training_predictions_and_roundtrips() -> None:
    from sklearn.ensemble import ExtraTreesRegressor

    from app.fx.agents.trained import ForestModel, forest_prediction

    rng = np.random.default_rng(7)
    x = rng.normal(size=(250, len(FEATURE_NAMES)))
    y = x[:, 0] * x[:, 1]
    fitted = ExtraTreesRegressor(n_estimators=3, max_depth=4, random_state=7).fit(x, y)
    trees = tuple(
        {
            "left": t.tree_.children_left.tolist(),
            "right": t.tree_.children_right.tolist(),
            "feature": t.tree_.feature.tolist(),
            "threshold": t.tree_.threshold.tolist(),
            "value": t.tree_.value[:, 0, 0].tolist(),
        }
        for t in fitted.estimators_
    )
    portable = ForestModel(**{**model().__dict__, "algorithm": "forest", "trees": trees})
    np.testing.assert_allclose(forest_prediction(x, trees), fitted.predict(x), atol=1e-12)
    restored = LinearModel.from_dict(json.loads(json.dumps(portable.to_dict())))
    assert isinstance(restored, ForestModel)
    np.testing.assert_array_equal(forest_prediction(x, restored.trees), forest_prediction(x, trees))
    bad = dict(trees[0])
    bad["left"] = [0, *bad["left"][1:]]
    with pytest.raises(ValueError, match="cyclic"):
        replace(portable, trees=(bad,))


def test_legacy_feature_models_remain_loadable() -> None:
    from app.fx.agents.trained import BASE_FEATURE_NAMES

    n = len(BASE_FEATURE_NAMES)
    old = replace(
        model(),
        center=(0.0,) * n,
        scale=(1.0,) * n,
        weights=(0.0,) * n,
        feature_names=BASE_FEATURE_NAMES,
        schema="fx-linear-paper/1",
    )
    restored = LinearModel.from_dict(json.loads(json.dumps(old.to_dict())))
    assert restored == old
    df = mid_frame(resample(history(days=100), "1h"))
    assert len(model_signals(df, restored)) == len(df)


def test_candle_features_include_context_and_exclude_volume() -> None:
    from app.fx.agents.trained import CANDLE_GEOMETRY

    df = mid_frame(resample(history(days=100), "1h"))
    f = feature_frame(df, "H1")
    assert len(FEATURE_NAMES) == 35
    assert not any("volume" in name for name in FEATURE_NAMES)
    patterns = [n for n in FEATURE_NAMES if n.startswith("candle_")]
    assert len(patterns) == 20
    assert ((f[patterns] >= 0) & (f[patterns] <= 1)).all().all()
    pd.testing.assert_frame_equal(
        f[list(CANDLE_GEOMETRY)].iloc[-20:],
        feature_frame(df.iloc[-260:], "H1")[list(CANDLE_GEOMETRY)].iloc[-20:],
    )


def test_same_wick_shape_has_different_context_features() -> None:
    # Synthetic shape is a feature plumbing test, never market evidence.
    from tests.unit.candles.helpers import bars, trend_prefix

    hammer = (106.0, 106.6, 103.0, 106.5)
    down = bars([*trend_prefix("down", 107.0), hammer]).drop(columns="volume")
    up = bars([*trend_prefix("up", 107.0), hammer]).drop(columns="volume")
    d, u = feature_frame(down, "H1").iloc[-1], feature_frame(up, "H1").iloc[-1]
    assert d["candle_hammer"] > 0 and d["candle_hanging_man"] == 0
    assert u["candle_hanging_man"] > 0 and u["candle_hammer"] == 0


def test_replacement_skips_models_already_working_in_other_slots() -> None:
    reg = registry()
    reg["models"].append({"model_id": "fresh"})
    reg["ranking"].append({"model_id": "fresh", "paper_candidate": True})
    result = retirement_decision(reg, "current", [-1.0] * 5, [], ["current", "winner"])
    assert result["replacement_id"] == "fresh"
    assert (
        retirement_decision(reg, "current", [-1.0] * 5, ["fresh"], ["winner"])["replacement_id"]
        is None
    )
