import numpy as np
import pandas as pd
import pytest

from app.fx.research.m5_recency_training import recency_weights


def test_half_life_and_specialty_weights():
    idx = pd.DatetimeIndex(["2023-01-01", "2023-06-30"], tz="UTC")
    out = recency_weights(idx, pd.DataFrame(), np.ones(2, dtype=bool), np.ones(2))
    assert out[1] / out[0] == pytest.approx(2)
    out_role = recency_weights(idx, pd.DataFrame(), np.ones(2, dtype=bool), np.array([5.0, 1.0]))
    assert out_role[0] / out[0] == pytest.approx(5)


def test_ineligible_future_and_outcomes_do_not_affect_train_weights():
    idx = pd.date_range("2023-01-01", periods=10, freq="D", tz="UTC")
    mask = np.arange(10) < 5
    original = recency_weights(idx, pd.DataFrame({"net_r": 0.0}, index=idx), mask, np.ones(5))
    changed = idx[:5].append(pd.date_range("2030-01-01", periods=5, freq="D", tz="UTC"))
    np.testing.assert_array_equal(
        original, recency_weights(changed, pd.DataFrame(), mask, np.ones(5))
    )


def test_empty_rejected():
    with pytest.raises(ValueError):
        recency_weights(
            pd.DatetimeIndex([], tz="UTC"), pd.DataFrame(), np.array([], dtype=bool), np.array([])
        )
