import numpy as np
import pandas as pd
import pytest

from app.fx.research.m5_magnitude_training import magnitude_weights


def test_exact_clipped_magnitude_keeps_specialty_ratio():
    idx = pd.date_range("2023-01-01", periods=4, tz="UTC")
    target = pd.DataFrame({"net_r": [0.0, -1.0, 100.0, 2.0]}, index=idx)
    specialty = np.array([5.0, 1.0, 5.0, 1.0])
    expected = np.array([0.25, 1.0, 3.0, 2.0])
    expected = expected / expected.mean() * specialty
    np.testing.assert_allclose(
        magnitude_weights(idx, target, np.ones(4, bool), specialty), expected
    )


def test_future_or_ineligible_outcomes_do_not_change_weights():
    idx = pd.date_range("2023-01-01", periods=4, tz="UTC")
    mask = np.array([True, True, False, False])
    target = pd.DataFrame({"net_r": [1.0, -2.0, 99.0, 99.0]}, index=idx)
    original = magnitude_weights(idx, target, mask, np.ones(2))
    target.iloc[2:, 0] = np.nan
    np.testing.assert_array_equal(original, magnitude_weights(idx, target, mask, np.ones(2)))


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_invalid_eligible_target_refuses(bad):
    idx = pd.date_range("2023-01-01", periods=1, tz="UTC")
    with pytest.raises(ValueError):
        magnitude_weights(
            idx, pd.DataFrame({"net_r": [bad]}, index=idx), np.ones(1, bool), np.ones(1)
        )


def test_alignment_and_empty_refuse():
    idx = pd.date_range("2023-01-01", periods=2, tz="UTC")
    target = pd.DataFrame({"net_r": [1.0, 2.0]}, index=idx)
    for mask, weights in [(np.zeros(2, bool), np.ones(0)), (np.ones(2, bool), np.ones(1))]:
        with pytest.raises(ValueError):
            magnitude_weights(idx, target, mask, weights)
    with pytest.raises(ValueError):
        magnitude_weights(idx, target.iloc[::-1], np.ones(2, bool), np.ones(2))


def test_protocol_preserves_frozen_gates_costs_and_six_candidates(monkeypatch):
    from pathlib import Path

    from app.fx.research import m5_magnitude_training as mod

    inherited = {
        "variants": {r: [3.0, 5.0] for r in ("reversao", "continuacao", "impulso")},
        "base_protocol": {"costs": "unchanged", "min_trades": 60},
        "thresholds": {"net_r_regression": 0.1, "positive_sell_probability": 0.55},
    }
    monkeypatch.setattr(mod.economic, "protocol", lambda _: inherited.copy())
    result = mod.protocol(Path("synthetic.pdf"))
    for key in ("variants", "base_protocol", "thresholds"):
        assert result[key] == inherited[key]
    assert sum(map(len, result["variants"].values())) == 6
    assert "half_life_days" not in result
    assert result["min_magnitude_r"] == 0.25 and result["max_magnitude_r"] == 3.0
    assert set(result["frozen_source_sha256"]) == set(mod.frozen_sources())
    assert all(len(x) == 64 for x in result["frozen_source_sha256"].values())
