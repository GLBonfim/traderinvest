# Statistical Validation (Phase 9)

> **Read this first.**
> - Statistical significance is **not** economic significance.
> - Historical significance does **not** imply future profitability.
> - One instrument (SPY) and one historical path are insufficient for generalisation.
> - The strategy parameters were not optimised, but the choice of what to study is not
>   independent of general market knowledge.
> - Multiple comparisons increase false-discovery risk; only the pre-declared formal family has
>   p-values, and only multiplicity-adjusted values should be read.
> - Bootstrap assumptions (block length, stationarity within the slice) matter.
> - Transaction costs are assumptions, not broker quotes. All backtests remain hypothetical.
>
> Everything below is **exploratory and descriptive**. Nothing is a ranking, a selection, or a
> claim that any rule works.

Code: `app/validation/` (`config.py`, `resampling.py`, `metrics.py`, `comparisons.py`,
`models.py`, `engine.py`, `cli.py`) · validation version `1.0.0`.

## What is validated

The six Phase 7 baselines and the two Phase 8 benchmarks, using the Phase 8 backtester
unchanged (next-session-open execution; raw prices for strategies and the price benchmark;
adjusted prices only for the total-return benchmark; dividends not credited to strategies).
Every input is recomputed from bars; Phase 7/8 objects are never modified.

## Pre-declared configuration (fixed before any result was seen)

| Item | Value |
|---|---|
| Resampling | non-circular moving-block bootstrap |
| Block length L | 21 sessions (≈ one trading month; ≈ n^(1/3) = 20.4 for n = 8,479) |
| Resamples B | 2,000 |
| Interval | percentile, 95% (2.5% / 97.5% quantiles of the finite bootstrap values) |
| Seed | 20261007 (numpy PCG64) |
| Periods/year | 252; rf = 0 |
| Slices | `full` (all), `early` (≤ 2009-12-31), `late` (≥ 2010-01-01) |
| Cost scenarios | A 0 bps + 0/order · B 5 bps + 0/order · C 10 bps + 0/order · D Phase 8 default (2 bps slippage + ½ of 2 bps spread + 1.00/order) |
| Formal family | 30 tests on `full`, scenario D (below) |

All of it is in `ValidationConfig`; every result records version, config fingerprint, method,
block length, resample count, confidence, seed, slice (first/last session, sessions), cost
scenario and the source backtest fingerprint.

## Moving-block bootstrap (Künsch 1989)

For a slice of n daily returns:

1. candidate blocks are the n − L + 1 contiguous windows `[i, i+L−1]`, i = 0 … n−L, all
   **inside the slice**. Sampling is **non-circular**: no block wraps from the slice's last
   session to its first, so the slice boundary is never crossed and no artificial adjacency is
   created;
2. each resample draws ⌈n/L⌉ block starts uniformly with replacement, concatenates the blocks in
   draw order and truncates to exactly n observations;
3. the **same index matrix** is applied to every series of the slice (net returns of the 8
   subjects and gross returns of the 6 strategies) — comparisons are **paired/time-aligned**,
   preserving the dependence between strategies and benchmarks.

Each metric is recomputed on each resample with the Phase 8 definitions (tested to be
identical on the observed sample). Known properties: edge sessions (within L−1 of a slice
boundary) are under-represented; dependence longer than L is broken.

Interpretation per metric:

| Metric | Bootstrap interpretation |
|---|---|
| annualised mean return, volatility, Sharpe, Sortino | standard (smooth functions of means) |
| cumulative return, CAGR | compounding over resampled block order; intervals are wide and skewed |
| max drawdown | **approximate**: path-dependent, block resampling preserves only within-block paths |
| Sortino | resamples without negative returns are undefined and excluded (`valid_resamples`) |

## Comparisons

All differences are `a − b` of the same metric on the **same resampled sessions**.

**Formal family (30 tests, slice `full`, scenario D)** — H0: paired difference = 0:

- each timing strategy (`sma_trend`, `sma_crossover`, `rsi_momentum`, `price_action_trend`,
  `regime_trend`) vs `benchmark_buy_and_hold_price` — 5 pairs;
- every pair of the 5 timing strategies — 10 pairs (includes the redundant pair);
- metrics: annualised mean daily return difference, Sharpe difference → 15 × 2 = 30.

`buy_and_hold` is excluded (it is the price benchmark's rule; difference identically 0).

p-value: two-sided centred bootstrap, `p = (1 + #{|θ*_b − θ̂| ≥ |θ̂|}) / (B + 1)` (minimum
≈ 0.0005 for B = 2,000). Corrections over the declared family size m = 30:

- **Holm** (primary): controls the family-wise error rate under arbitrary dependence; the
  strategies are strongly dependent (same instrument, overlapping positions), so a method
  without independence assumptions is the safer primary choice.
- **Benjamini–Hochberg** (secondary): controls the false-discovery rate under independence or
  positive regression dependence; reported for context.
- Undefined tests (e.g. zero-variance series) count in m and are reported as undefined.

**Descriptive comparisons** (intervals only, no p-values): vs `benchmark_buy_and_hold_total`
(mixes price-only strategies with a dividend-reinvested benchmark), `buy_and_hold` vs the price
benchmark, gross vs net (cost drag), cumulative/CAGR differences, and every comparison on the
`early`/`late` slices.

## Temporal slices

`early` and `late` use fixed calendar dates (not a data-dependent midpoint), so a closed slice
does not change when data is appended. For each slice, strategy features are computed on bars
≤ slice end only, the backtest window is [start, end] with fresh capital, and resampling stays
inside the slice. The slices are **not** independent datasets, are not train/validation/test
sets, and no parameter is fitted on any of them.

## Point-in-time guarantees (tested)

Closed slice unchanged when data is appended; future mutation of OHLC, volume or `adj_close`
does not change a closed slice; future strategy-state mutation does not change slice
statistics; an extra cost scenario does not change other results; inputs are never modified;
resampled indices stay inside the slice; a leaky validator that ignores the slice end is
detected; same seed ⇒ identical output, different seed ⇒ different draws.

## Results — SPY daily, 8,479 sessions (1993-01-29 → 2026-10-06)

### Uncertainty intervals (full slice, scenario D, 95%)

| Subject | CAGR [95% CI] | Sharpe [95% CI] | Ann. vol [95% CI] | Max DD (approx.) [95% CI] |
|---|---|---|---|---|
| buy_and_hold / price benchmark | 8.9% [3.1, 14.7] | 0.55 [0.26, 0.87] | 18.6% [16.9, 20.5] | −56.5% [−65.8, −29.3] |
| total-return benchmark | 10.9% [5.0, 16.8] | 0.65 [0.35, 0.97] | 18.5% [16.9, 20.4] | −55.2% [−62.0, −28.2] |
| sma_trend | 6.6% [2.7, 10.7] | 0.60 [0.28, 0.93] | 11.8% [11.1, 12.5] | −30.0% [−48.8, −19.5] |
| sma_crossover | 4.5% [1.0, 7.9] | 0.44 [0.14, 0.73] | 11.7% [11.0, 12.4] | −37.6% [−51.0, −19.9] |
| rsi_momentum | 2.7% [−0.9, 6.5] | 0.30 [−0.03, 0.63] | 10.9% [10.3, 11.4] | −40.0% [−61.0, −22.4] |
| price_action_trend | 1.7% [−0.2, 3.6] | 0.30 [−0.00, 0.63] | 6.4% [5.5, 7.3] | −24.4% [−41.0, −11.6] |
| regime_trend | 1.7% [−0.2, 3.6] | 0.30 [0.00, 0.62] | 6.4% [5.5, 7.3] | −24.4% [−41.2, −11.6] |

Cumulative-return intervals are very wide (e.g. buy & hold 16.7 [1.8, 101.0]): compounding
over 33 years amplifies resampling uncertainty.

### Formal family (30 tests; Holm primary)

3 of 30 tests have Holm-adjusted p < 0.05 and 5 of 30 BH-adjusted p < 0.05 — **all of them
on annualised mean-return differences; no Sharpe difference is below 0.05 after either
correction.**

| Comparison (a − b) | Metric | Estimate [95% CI] | p | p Holm | p BH |
|---|---|---|---|---|---|
| regime_trend − price benchmark | ann. mean return | −8.4% [−12.9, −3.3] | 0.0015 | 0.045 | 0.015 |
| sma_trend − price_action_trend | ann. mean return | +5.2% [+1.9, +8.2] | 0.0015 | 0.045 | 0.015 |
| sma_trend − regime_trend | ann. mean return | +5.2% [+1.9, +8.2] | 0.0015 | 0.045 | 0.015 |
| price_action_trend − price benchmark | ann. mean return | −8.4% [−12.9, −3.3] | 0.0025 | 0.065 | 0.015 |
| rsi_momentum − price benchmark | ann. mean return | −7.0% [−10.7, −2.8] | 0.0020 | 0.054 | 0.015 |
| sma_trend − price benchmark | Sharpe | +0.05 [−0.21, +0.31] | 0.71 | 1.00 | 0.82 |
| price_action_trend − regime_trend | Sharpe | +0.002 [−0.016, +0.021] | 0.77 | 1.00 | 0.83 |

(Full table: `uv run python -m app.validation.cli run --symbol SPY`.)

Interpretation (exploratory): the mean-return differences that survive correction compare
series with very different **exposure** (19% vs 100% invested); lower average return for a
mostly-uninvested rule is largely mechanical and says nothing about risk-adjusted value. On
Sharpe, every interval vs the price benchmark includes 0 and no adjusted p-value is below 0.05:
**the data do not distinguish the risk-adjusted performance of any timing baseline from buy &
hold, or from each other.** This is "not conclusive", not "equal".

### Strategy vs total-return benchmark (descriptive)

All six strategies have negative point estimates of CAGR difference vs the total-return
benchmark (−2.0% for buy & hold = the dividend gap, to −9.2%); Sharpe-difference intervals
include 0 for `sma_trend` and `sma_crossover`, and are mostly below 0 for the others. These
comparisons mix price-only strategies with a dividend-reinvested benchmark and carry no
p-values.

### Cost sensitivity (full slice; predefined scenarios; trades identical across scenarios)

| Strategy | Sharpe A / B / C / D | CAGR A / B / C / D | Cum. return A → C | Total costs C |
|---|---|---|---|---|
| buy_and_hold | 0.55 / 0.55 / 0.55 / 0.55 | 8.92% / 8.92 / 8.92 / 8.92 | 16.72 → 16.70 | 100 |
| sma_trend | 0.62 / 0.59 / 0.56 / 0.60 | 6.84 / 6.48 / 6.13 / 6.62 | 8.26 → 6.41 | 58,182 |
| sma_crossover | 0.45 / 0.43 / 0.40 / 0.44 | 4.67 / 4.39 / 4.11 / 4.50 | 3.64 → 2.88 | 32,229 |
| rsi_momentum | 0.38 / 0.25 / 0.12 / 0.30 | 3.62 / 2.17 / 0.74 / 2.73 | 2.31 → 0.28 | 99,465 |
| price_action_trend | 0.32 / 0.29 / 0.26 / 0.30 | 1.84 / 1.66 / 1.48 / 1.73 | 0.85 → 0.64 | 17,595 |
| regime_trend | 0.32 / 0.29 / 0.26 / 0.30 | 1.83 / 1.65 / 1.48 / 1.72 | 0.84 → 0.64 | 17,166 |

`rsi_momentum` (≈28× annual turnover, 947 orders) is by far the most cost-sensitive: its
Sharpe falls from 0.38 to 0.12 between 0 and 10 bps, its CAGR from 3.6% to 0.7%. Gross-vs-net
intervals (scenario D) confirm cost drag is clearly non-zero for every timing rule
(e.g. `rsi_momentum` CAGR drag 0.89% [0.82, 0.96]).

### Slices (Sharpe point [95% CI])

| Subject | early (≤ 2009) | late (≥ 2010) |
|---|---|---|
| price benchmark | 0.38 [−0.02, 0.81] | 0.76 [0.34, 1.23] |
| sma_trend | 0.55 [0.13, 0.99] | 0.65 [0.15, 1.16] |
| sma_crossover | 0.33 [−0.07, 0.75] | 0.55 [0.10, 1.00] |
| rsi_momentum | 0.18 [−0.26, 0.64] | 0.43 [−0.08, 0.96] |
| price_action_trend | 0.51 [0.18, 0.86] | 0.12 [−0.33, 0.68] |
| regime_trend | 0.50 [0.18, 0.86] | 0.12 [−0.33, 0.68] |

Point estimates move substantially between slices (e.g. price-action/regime 0.51 → 0.12) and
intervals overlap widely — the observations are not stable enough to support conclusions.

### Redundancy

| Slice | Position agreement | Daily-return correlation | Sessions with different returns* |
|---|---|---|---|
| full | 99.906% | 0.998 | 126 / 8,479 |
| early | 99.812% | 0.996 | 62 / 4,264 |
| late | 100% | 1.000 | 0 / 4,215 |

\* |difference| > 1e−12 (fully-invested accounting leaves ~1e−11 cash residues; identical
positions differ at ~1e−16). Their paired Sharpe difference is 0.002 [−0.016, 0.021]. Both are
kept; for multiple-comparison purposes they are effectively one hypothesis counted twice,
which makes the Holm correction slightly conservative.

### Concentration (full slice, scenario D)

| Subject | Cum. return | Without its 10 best days | Top-5 trades' share of winning P&L |
|---|---|---|---|
| buy_and_hold | 16.71 | 6.69 | — |
| sma_trend | 7.65 | 4.92 | 58% |
| sma_crossover | 3.40 | 1.86 | 39% |
| rsi_momentum | 1.47 | 0.75 | 17% |
| price_action_trend | 0.78 | 0.28 | 40% |
| regime_trend | 0.77 | 0.27 | 40% |

Removing the 10 best of 8,479 sessions removes roughly half to two-thirds of every
cumulative return: results depend heavily on few observations.

Runtime: ≈ 28–31 s for the complete default validation (3 slices × 4 scenarios, 2,000
resamples), of which bootstrap ≈ 11.5 s, backtests ≈ 13.7 s, strategy features ≈ 5.4 s.

## Limitations

- One instrument, one path; slices overlap in method and are not independent.
- Block bootstrap assumes approximate stationarity within each slice and preserves dependence
  only up to L = 21 sessions; regime persistence longer than that is broken.
- Percentile intervals can be biased for skewed statistics (cumulative return, drawdown);
  BCa or studentised intervals were not implemented.
- The centred bootstrap Sharpe-difference test is approximate (Ledoit–Wolf HAC-based tests are a
  known alternative, not implemented).
- Costs remain generic assumptions; scenario D is not "the" true cost.
- Dividends are not credited to timing strategies (Phase 8 convention, unchanged).
