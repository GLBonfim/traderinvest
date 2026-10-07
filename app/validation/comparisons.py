"""Pre-declared comparison families and multiple-testing corrections.

FORMAL family (evaluated only on the formal slice, default cost scenario) — declared before
any result was seen:
  * each of the 5 timing strategies vs the price-return benchmark          (5 pairs)
  * every pair of the 5 timing strategies                                   (10 pairs)
  each on 2 metrics: annualized mean daily return difference and Sharpe difference
  => 30 tests. H0 for each: the paired difference of the metric equals 0.
  buy_and_hold is excluded from formal tests: it is the price benchmark's rule (identical).
DESCRIPTIVE comparisons (intervals only, no p-values): vs the total-return benchmark (mixes
price-only strategies with a dividend-reinvested benchmark), buy_and_hold vs the price
benchmark, gross vs net, cumulative/CAGR differences, and everything on non-formal slices.
"""

from itertools import combinations

import numpy as np

TIMING = ("sma_trend", "sma_crossover", "rsi_momentum", "price_action_trend", "regime_trend")
ALL_STRATEGIES = ("buy_and_hold", *TIMING)
PRICE_BENCH = "benchmark_buy_and_hold_price"
TOTAL_BENCH = "benchmark_buy_and_hold_total"
FORMAL_METRICS = ("annualized_mean_return", "sharpe")
DESCRIPTIVE_METRICS = ("cumulative_return", "cagr", "sharpe", "annualized_mean_return")


def formal_pairs() -> list[tuple[str, str]]:
    return [(s, PRICE_BENCH) for s in TIMING] + list(combinations(TIMING, 2))


def descriptive_pairs() -> list[tuple[str, str]]:
    return (
        [(s, TOTAL_BENCH) for s in ALL_STRATEGIES]
        + [("buy_and_hold", PRICE_BENCH)]
        + [(f"{s}__gross", s) for s in ALL_STRATEGIES]
    )


def holm(p: np.ndarray, m: int | None = None) -> np.ndarray:
    """Holm step-down adjusted p-values (FWER; valid under any dependence).

    `m` = declared family size (default: len(p)). Undefined tests (NaN) still count towards
    `m` — dropping them would shrink the family and make the correction less conservative —
    and their adjusted value stays NaN.
    """
    m = len(p) if m is None else m
    out = np.full(p.shape, np.nan)
    ok = np.flatnonzero(np.isfinite(p))
    order = ok[np.argsort(p[ok], kind="stable")]
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, min(1.0, (m - rank) * p[i]))
        out[i] = running
    return out


def benjamini_hochberg(p: np.ndarray, m: int | None = None) -> np.ndarray:
    """Benjamini-Hochberg adjusted p-values (FDR; valid under independence or positive
    regression dependence). `m` = declared family size; NaN tests count towards it."""
    m = len(p) if m is None else m
    out = np.full(p.shape, np.nan)
    ok = np.flatnonzero(np.isfinite(p))
    order = ok[np.argsort(p[ok], kind="stable")]
    running = 1.0
    for rank in range(order.size - 1, -1, -1):
        i = order[rank]
        running = min(running, p[i] * m / (rank + 1))
        out[i] = min(1.0, running)
    return out
