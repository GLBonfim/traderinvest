"""Non-circular moving-block bootstrap (Künsch 1989) for daily return series.

For a slice with n observations and block length L (1 <= L <= n):
- candidate blocks are the n - L + 1 contiguous windows [i, i + L - 1], i = 0 .. n - L, all
  entirely inside the slice (NON-circular: no block wraps from the end of the slice to its
  start, so no artificial adjacency between the last and first sessions is created);
- each resample draws ceil(n / L) block starts uniformly with replacement, concatenates the
  blocks in draw order and truncates to exactly n observations;
- the SAME index matrix is applied to every series of the slice (paired / time-aligned
  resampling), preserving cross-sectional dependence between strategies and benchmarks.
Known property: observations within L - 1 of either end of the slice appear in fewer
candidate blocks (edge under-representation).

Randomness: numpy PCG64 generator seeded with the configured seed (deterministic).
"""

import numpy as np


def moving_block_indices(n: int, block_length: int, n_resamples: int, seed: int) -> np.ndarray:
    """(n_resamples, n) int array of positions into a slice of length n."""
    if n < 1:
        raise ValueError("slice must contain at least one observation")
    if not 1 <= block_length <= n:
        raise ValueError(f"block_length must be in [1, {n}] for a slice of {n} observations")
    if n_resamples < 1:
        raise ValueError("n_resamples must be >= 1")
    rng = np.random.default_rng(seed)
    n_blocks = -(-n // block_length)  # ceil
    starts = rng.integers(0, n - block_length + 1, size=(n_resamples, n_blocks))
    offsets = np.arange(block_length)
    idx = (starts[:, :, None] + offsets[None, None, :]).reshape(
        n_resamples, n_blocks * block_length
    )
    return idx[:, :n]


def percentile_interval(samples: np.ndarray, confidence: float) -> tuple[float, float, int]:
    """Percentile interval of the finite samples; returns (lower, upper, n_finite)."""
    finite = samples[np.isfinite(samples)]
    if finite.size == 0:
        return float("nan"), float("nan"), 0
    alpha = 1 - confidence
    lo, hi = np.quantile(finite, [alpha / 2, 1 - alpha / 2])
    return float(lo), float(hi), int(finite.size)


def centered_p_value(samples: np.ndarray, estimate: float) -> float:
    """Two-sided bootstrap p-value for H0: parameter = 0, from the centred distribution:
        p = (1 + #{ |θ*_b - θ̂| >= |θ̂| }) / (B + 1)
    (the bootstrap distribution shifted to the null approximates the sampling distribution
    under H0). NaN if θ̂ or all samples are undefined."""
    finite = samples[np.isfinite(samples)]
    if finite.size == 0 or not np.isfinite(estimate):
        return float("nan")
    extreme = np.count_nonzero(np.abs(finite - estimate) >= abs(estimate))
    return float((1 + extreme) / (finite.size + 1))
