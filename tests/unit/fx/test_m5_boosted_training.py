"""Portable boosted research models must reproduce fitted scores; never executable pickle."""

import json

import numpy as np
import pytest

from app.fx.research.m5_boosted_training import fitted_model, portable_predict
from app.fx.research.m5_lesson_training import NAMES


@pytest.mark.parametrize("role", ["reversao", "continuacao", "impulso"])
def test_boosted_export_is_finite_offline_and_json_roundtrip_matches(role):
    rng = np.random.default_rng(7)
    x = rng.normal(size=(700, len(NAMES)))
    y = 1.5 * x[:, 0] * x[:, 1] + rng.normal(size=700) * 0.05
    fit, artifact = fitted_model(role, 3, x, y, sample_weight=np.ones(len(y)))
    assert artifact["export_max_abs_error"] < 1e-10
    assert not artifact["execution_enabled"]
    serialized = json.loads(json.dumps(artifact, allow_nan=False))
    np.testing.assert_allclose(portable_predict(fit, x), portable_predict(serialized, x))
    assert np.std(portable_predict(serialized, x)) > 0
    if role == "impulso":
        assert (
            (portable_predict(serialized, x) >= 0) & (portable_predict(serialized, x) <= 1)
        ).all()


def test_unfrozen_variant_and_bad_export_inputs_rejected():
    x = np.ones((400, len(NAMES)))
    y = np.tile([-1.0, 1.0], 200)
    with pytest.raises(ValueError):
        fitted_model("reversao", 10, x, y)
    fit, _ = fitted_model("reversao", 3, x, y)
    x[0, 0] = np.nan
    with pytest.raises(ValueError):
        portable_predict(fit, x)
