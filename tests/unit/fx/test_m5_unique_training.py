import numpy as np
import pandas as pd
import pytest

from app.fx.research.m5_unique_training import unique_weights


def test_isolated_events_keep_specialty_weight_and_outcomes_cannot_change_weights():
    idx = pd.date_range("2023-01-01", periods=12, freq="5min", tz="UTC")
    target = pd.DataFrame({"label_end": idx + pd.Timedelta(minutes=10), "net_r": 0.0}, index=idx)
    mask = np.zeros(12, dtype=bool)
    mask[[0, 4, 8]] = True
    w = np.array([5.0, 1.0, 1.0])
    np.testing.assert_allclose(unique_weights(idx, target, mask, w), w)
    target["net_r"] = 999
    np.testing.assert_allclose(unique_weights(idx, target, mask, w), w)


def test_overlapping_events_downweighted_and_ineligible_events_do_not_count():
    idx = pd.date_range("2023-01-01", periods=15, freq="5min", tz="UTC")
    target = pd.DataFrame({"label_end": idx + pd.Timedelta(minutes=15)}, index=idx)
    mask = np.zeros(15, dtype=bool)
    mask[[0, 1, 9]] = True
    got = unique_weights(idx, target, mask, np.ones(3))
    assert got[0] == pytest.approx(got[1])
    assert got[2] > got[0]
    assert got.mean() == pytest.approx(1)
    target.loc[idx[~mask], "label_end"] = idx[-1]
    np.testing.assert_allclose(unique_weights(idx, target, mask, np.ones(3)), got)


def test_invalid_horizon_rejected():
    idx = pd.date_range("2023-01-01", periods=3, freq="5min", tz="UTC")
    target = pd.DataFrame({"label_end": idx}, index=idx)
    with pytest.raises(ValueError):
        unique_weights(idx, target, np.ones(3, dtype=bool), np.ones(3))
