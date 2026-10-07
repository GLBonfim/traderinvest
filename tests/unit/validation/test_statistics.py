"""Bootstrap correctness, metric equivalence with Phase 8, multiple-testing corrections."""

import math

import numpy as np
import pandas as pd
import pytest

from app.backtest.metrics import return_metrics, returns_from_equity
from app.validation import comparisons as cmp
from app.validation.metrics import concentration, matrix_metrics
from app.validation.resampling import centered_p_value, moving_block_indices, percentile_interval

# ── moving-block indices ──


def test_shape_range_and_non_circular_blocks() -> None:
    n, block = 103, 10
    idx = moving_block_indices(n, block, 50, seed=1)
    assert idx.shape == (50, n)
    assert idx.min() >= 0 and idx.max() <= n - 1
    for row in idx:
        for start in range(0, n, block):
            chunk = row[start : start + block]
            # contiguous and never wrapping from the end of the slice to its start
            assert (np.diff(chunk) == 1).all()
            assert chunk[0] <= n - block


def test_deterministic_seed() -> None:
    a = moving_block_indices(500, 21, 30, seed=7)
    np.testing.assert_array_equal(a, moving_block_indices(500, 21, 30, seed=7))
    assert not np.array_equal(a, moving_block_indices(500, 21, 30, seed=8))


def test_block_length_extremes() -> None:
    whole = moving_block_indices(40, 40, 5, seed=0)
    assert (whole == np.arange(40)).all()  # one block = the original order
    iid = moving_block_indices(40, 1, 5, seed=0)
    assert iid.shape == (5, 40)


@pytest.mark.parametrize(("n", "block", "b"), [(10, 0, 5), (10, 11, 5), (0, 1, 5), (10, 2, 0)])
def test_invalid_resampling_parameters(n: int, block: int, b: int) -> None:
    with pytest.raises(ValueError):
        moving_block_indices(n, block, b, seed=0)


def test_bootstrap_mean_is_close_to_sample_mean() -> None:
    x = np.random.default_rng(3).normal(0.001, 0.01, 2000)
    idx = moving_block_indices(len(x), 20, 2000, seed=4)
    assert x[idx].mean(axis=1).mean() == pytest.approx(x.mean(), abs=2e-4)


def test_interval_coverage_sanity_on_iid_data() -> None:
    """95% percentile intervals for the mean of N(0.5, 1) samples cover 0.5 most of the time."""
    rng = np.random.default_rng(5)
    covered, sims = 0, 150
    for s in range(sims):
        x = rng.normal(0.5, 1.0, 120)
        idx = moving_block_indices(len(x), 4, 400, seed=s)
        lo, hi, _ = percentile_interval(x[idx].mean(axis=1), 0.95)
        covered += lo <= 0.5 <= hi
    assert 0.85 <= covered / sims <= 0.99


def test_percentile_interval_and_nan_handling() -> None:
    samples = np.arange(1, 1001, dtype=float)
    lo, hi, n = percentile_interval(samples, 0.95)
    assert (lo, hi, n) == pytest.approx(
        (np.quantile(samples, 0.025), np.quantile(samples, 0.975), 1000)
    )
    lo, hi, n = percentile_interval(np.array([np.nan, 1.0, 2.0, np.inf]), 0.9)
    assert n == 2
    assert math.isnan(percentile_interval(np.array([np.nan]), 0.95)[0])


def test_centered_p_value() -> None:
    samples = np.random.default_rng(6).normal(0.0, 1.0, 999)
    assert centered_p_value(samples, 0.0) == 1.0
    tight = np.full(999, 5.0)
    assert centered_p_value(tight, 5.0) == pytest.approx(1 / 1000)  # nothing as extreme
    assert math.isnan(centered_p_value(samples, float("nan")))


# ── metrics equal Phase 8 definitions ──


def test_matrix_metrics_match_phase8() -> None:
    rng = np.random.default_rng(7)
    r = rng.normal(0.0004, 0.012, (500, 3))
    m = matrix_metrics(r, 252)
    for j in range(3):
        eq = pd.Series(100 * np.cumprod(1 + r[:, j]))
        ref = return_metrics(returns_from_equity(eq, 100.0), eq, 100.0, 252, 0.0)
        for k in (
            "cumulative_return",
            "cagr",
            "annualized_volatility",
            "sharpe",
            "sortino",
            "max_drawdown",
        ):
            assert m[k][j] == pytest.approx(ref[k], rel=1e-9, abs=1e-12), k
    assert m["annualized_mean_return"][0] == pytest.approx(r[:, 0].mean() * 252)


def test_matrix_metrics_undefined_cases() -> None:
    m = matrix_metrics(np.zeros((10, 1)), 252)
    assert math.isnan(m["sharpe"][0]) and math.isnan(m["sortino"][0])
    assert m["cumulative_return"][0] == 0 and m["max_drawdown"][0] == 0


def test_concentration_exact() -> None:
    r = np.array([0.1, -0.05, 0.02, 0.0])
    c = concentration(r, top_days=1)
    assert c["cumulative_return"] == pytest.approx(1.1 * 0.95 * 1.02 - 1)
    assert c["cumulative_return_without_best_1_days"] == pytest.approx(0.95 * 1.02 - 1)


# ── families and corrections ──


def test_formal_family_is_declared_and_counted() -> None:
    pairs = cmp.formal_pairs()
    assert len(pairs) == 15 and len(pairs) * len(cmp.FORMAL_METRICS) == 30
    assert ("price_action_trend", "regime_trend") in pairs  # redundant pair is counted
    assert all(a != "buy_and_hold" for a, _ in pairs)


def test_holm_and_bh_known_values() -> None:
    p = np.array([0.01, 0.04, 0.03, 0.005])
    np.testing.assert_allclose(cmp.holm(p), [0.03, 0.06, 0.06, 0.02])
    np.testing.assert_allclose(cmp.benjamini_hochberg(p), [0.02, 0.04, 0.04, 0.02])
    with_nan = cmp.holm(np.array([0.01, np.nan, 0.02]))  # NaN still counts: m = 3
    assert math.isnan(with_nan[1])
    assert (with_nan[0], with_nan[2]) == pytest.approx((0.03, 0.04))
    assert (cmp.holm(np.array([0.5, 0.9])) <= 1).all()


def test_undefined_tests_still_count_in_family_size() -> None:
    p = np.array([0.01, np.nan, np.nan])
    assert cmp.holm(p, m=3)[0] == pytest.approx(0.03)  # not 0.01 (m would shrink to 1)
    assert cmp.benjamini_hochberg(p, m=3)[0] == pytest.approx(0.03)
