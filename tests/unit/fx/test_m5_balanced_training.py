import numpy as np
import pandas as pd
import pytest

from app.fx.research.m5_balanced_training import balanced_weights


def test_half_mass_and_preserved_total():
    idx = pd.date_range("2023-01-01", periods=10, tz="UTC")
    weights = np.array([5.0, 5.0] + [1.0] * 8)
    out = balanced_weights(idx, pd.DataFrame(), np.ones(10, bool), weights)
    assert out[:2].sum() == pytest.approx(9)
    assert out[2:].sum() == pytest.approx(9)
    assert out.sum() == pytest.approx(weights.sum())


def test_future_outcomes_and_ineligible_rows_never_used():
    idx = pd.date_range("2023-01-01", periods=4, tz="UTC")
    out = balanced_weights(
        idx,
        pd.DataFrame({"net_r": [np.nan] * 4}),
        np.array([True, True, False, False]),
        np.array([5.0, 1.0]),
    )
    np.testing.assert_array_equal(out, [3.0, 3.0])


@pytest.mark.parametrize("weights", [[], [1.0, 1.0], [5.0, 5.0], [5.0, np.nan], [5.0, 2.0]])
def test_invalid_or_missing_group_refuses(weights):
    idx = pd.date_range("2023-01-01", periods=len(weights), tz="UTC")
    with pytest.raises(ValueError):
        balanced_weights(idx, pd.DataFrame(), np.ones(len(weights), bool), np.array(weights))
