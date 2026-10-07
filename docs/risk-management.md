# Risk Management (Phase 11)

> The risk layer is a **measurement and control layer**, not an alpha source. Reducing exposure
> usually reduces both risk **and** return; nothing below shows that any overlay is beneficial.
> Parameters are pre-declared conventions; none was tuned on historical performance or on the
> Phase 10 test results. No broker, no real money.

Code: `app/risk/` (`config.py`, `sizing.py`, `stops.py`, `limits.py`, `engine.py`,
`diagnostics.py`, `cli.py`) · risk version `1.0.0`.

## Architecture

```
strategy state (Phase 7, unchanged)
      ↓  requested_exposure  (LONG 1 / FLAT 0; INSUFFICIENT_DATA keeps the previous request)
sizing → sized_exposure
      ↓  limits (gross / single-instrument / notional caps; leverage impossible)
      ↓  stop lockout, drawdown lock, session lock
approved_exposure  (decision at the close of T; reasons recorded)
      ↓  execution at the open of the next XNYS session (Phase 8 convention, validated)
executed exposure  (rebalanced only if |actual − target| ≥ rebalance band 0.10)
```

The strategy signal is **never** modified: every decision row stores `strategy_state`,
`requested_exposure`, `sized_exposure`, `approved_exposure`, `target_exposure`,
`intervention` (approved ≠ requested), `reasons`, `risk_state`, drawdown, high-water mark,
stop level, stop flags, the volatility/ATR used, `observed_at` and `effective_at`. Each run
carries the risk and backtest fingerprints.

**Relationship to Phase 8.** Phase 8 represents positions {0, 1}. Fractional sizing needs
exposures in [0, 1], so `app/risk/engine.py` generalises the accounting **without changing
Phase 8**: it reuses `order_costs`, `buy_notional`, the metric functions and the
next-session-open validation. With the control scenario the equity curve is **identical** to
Phase 8 (max difference 0.0 on all six baselines over 8,479 SPY sessions, net and gross).

Per session t: (1) open of t — execute the target decided at the close of t−1; (2) close of t
— mark to market; (3) update monitors; (4) evaluate the stop on the close; (5) decide for the
next session using only data ≤ t. Gross metrics replay the same decided targets with zero costs.

## Sizing (exposure = fraction of current equity, capped at 1)

| Method | Formula (values at the close of T) |
|---|---|
| full | 1 |
| fixed_fraction | f |
| volatility_target | σ* / σ̂, σ̂ = `realized_vol_20` (annualised, Phase 5), σ* = target (annualised) |
| atr_risk | b · close / (k · ATR14): a fall of k·ATR costs b of equity |

Undefined or non-positive volatility/ATR → exposure 0 with reason `risk_input_undefined`
(never a default size). Orders: buying spends at most `buy_notional(cash)` (Phase 8, costs
included, no leverage); fractional shares as in Phase 8.

## Limits and locks

- `max_gross_exposure`, `max_single_instrument_exposure` (≤ 1), optional `max_position_notional`.
- **Drawdown**: drawdown = equity / HWM − 1 at the close (HWM starts at initial capital).
  WARNING at ≤ −warning, LOCKED at ≤ −lock. While LOCKED: `force_flat` exits and blocks entries,
  `block_entries` keeps the position and blocks increases. After `cooldown_sessions` decisions
  the lock is released and the HWM is reset to the current equity (otherwise a flat account
  could never recover and would stay locked forever). The reported `max_drawdown` metric keeps
  the Phase 8 definition (global HWM, never reset).
- **Session loss** (infrastructure, not enabled in any scenario): equity(T)/equity(T−1) − 1
  ≤ −limit blocks increases for N sessions; consecutive losing round trips ≥ K blocks entries.

## Stops on daily OHLC

- Level set once at entry: fixed % below the entry fill, or entry − k·ATR14 (ATR known at the
  entry decision).
- **Triggered only when the session close ≤ level**; the exit is decided at that close and
  executed at the next open (gap risk borne).
- **Daily OHLC never reveals intraday order.** A low ≤ level with a close above is reported as
  `stop_intraday_touch` and is **not** an exit; a bar whose range spans both far above and far
  below the level is treated the same way. Intraday stop orders are **unsupported** (they would
  require intraday data).
- Stop and strategy exit on the same close → one exit at the next open (stop flagged).
- After a stop, re-entry is blocked until the strategy signal resets (a FLAT request),
  avoiding immediate re-entry on a still-LONG signal.
- No take-profit (not required for loss/exposure control).

## Pre-declared scenarios (control first; no search)

| Scenario | Configuration |
|---|---|
| control_no_overlay | full exposure, no stop, no locks |
| fixed_fraction_50 | f = 0.5 |
| volatility_target_10 | σ* = 10% annualised |
| drawdown_lock_20 | warning −10%, lock −20%, force flat, cooldown 21 sessions |
| atr_stop_3 | stop at entry − 3 × ATR14 |
| atr_risk_1pct | b = 1%, k = 2 |

All: rebalance band 0.10, Phase 8 default costs, raw prices, no dividends, long-only.

## Results — SPY, full sample (8,479 sessions), descriptive only

| Strategy | Scenario | CAGR | Vol | Sharpe | Max DD | Orders | Costs | Req. / appr. / exec. exposure | Interventions |
|---|---|---|---|---|---|---|---|---|---|
| buy_and_hold | control | 8.9% | 18.6% | 0.55 | −56.5% | 1 | 31 | 1.00 / 1.00 / 1.00 | 0 |
| buy_and_hold | fixed 50% | 5.1% | 9.5% | 0.58 | −31.6% | 12 | 109 | 1.00 / 0.50 / 0.53 | 100% |
| buy_and_hold | vol target 10% | 6.3% | 10.4% | 0.64 | −33.1% | 567 | 7,759 | 1.00 / 0.73 / 0.72 | 73% |
| buy_and_hold | drawdown lock | 7.5% | 16.8% | 0.52 | **−59.4%** | 21 | 1,803 | 1.00 / 0.98 / 0.98 | 2.5% (10 locks) |
| buy_and_hold | ATR stop 3 | identical to control (stop never triggered) | | | | | | | 0 |
| buy_and_hold | ATR risk 1% | 3.7% | 6.1% | 0.63 | −18.3% | 279 | 2,182 | 1.00 / 0.44 / 0.43 | 99.5% |
| sma_trend | control | 6.6% | 11.8% | 0.60 | −29.9% | 223 | 19,454 | 0.73 / 0.73 / 0.73 | 0 |
| sma_trend | vol target 10% | 5.1% | 8.6% | 0.62 | −18.7% | 623 | 13,391 | 0.73 / 0.59 / 0.59 | 48% |
| sma_crossover | ATR stop 3 | 2.3% | 10.6% | **0.27** | −39.5% | 179 | 7,613 | 0.66 / 0.58 / 0.58 | 34 stops, 20 touches |
| rsi_momentum | vol target 10% | 2.3% | 7.3% | 0.34 | −22.7% | 1,203 | 31,696 | 0.66 / 0.52 / 0.51 | 43% |

(Full 6 × 6 × 3 table: `uv run python -m app.risk.cli run --symbol SPY` → `data/risk/risk_table.csv`.)

Paired block-bootstrap differences vs control (full slice, Phase 9 resampler, 95%,
**descriptive, no p-values; 360 intervals = 6 strategies × 5 overlays × 4 metrics × 3 slices**):

- Every sizing overlay reduces volatility with intervals excluding 0 (e.g. buy & hold vol target:
  −8.2 pp [−10.0, −6.7]) and reduces CAGR (e.g. −2.6 pp [−5.7, +0.6]).
- Sharpe differences vs control are small and their intervals include 0 for every sizing
  overlay (e.g. buy & hold vol target +0.09 [−0.05, +0.22]).
- Max-drawdown reductions are large for sizing overlays (approximate intervals; path-dependent).
- `drawdown_lock_20` on buy & hold: max drawdown worse (−2.9 pp [−9.1, +10.4]): the lock exits
  near a −20% drawdown and re-enters after 21 sessions, sometimes at worse prices.
- `atr_stop_3` on `sma_crossover`: Sharpe −0.17 [−0.31, −0.03] on this path (34 stops).
- `price_action_trend` / `regime_trend`: no drawdown-lock activity changed anything (identical to
  control); they behave alike under every overlay (redundancy persists).

Interpretation: sizing overlays trade return for lower volatility and drawdown roughly
proportionally; locks and stops are path-dependent and can hurt. None of this is evidence of
value, and multiplicity (360 descriptive intervals) applies to any later formal use.

## Point-in-time guarantees (tested)

Prefix vs full history (equity and decisions) for every scenario; future price and volume
mutation; future volatility/ATR (indicator) mutation; future strategy-state mutation; a sizing
input shifted from tomorrow is detected (control); control ≡ Phase 8 exactly; signals never
modified; no leverage, no negative cash, every fill at a session open after its signal.

Runtime: sizing ≈ 0.3 µs/call; one overlay on 8,479 bars (net + gross) ≈ 0.29–0.34 s; full
suite (6 × 6 × 3 slices + 360 bootstrap intervals) ≈ 60 s (inputs 8 s, overlays 23 s,
comparisons 29 s).

## Limitations

- Single instrument, long-only, daily bars: no portfolio/correlation limits, no intraday stops.
- Stops on closes: gap risk is realistic but intraday protection is not modelled.
- The drawdown-lock HWM reset after cooldown is a convention; other unlock rules behave
  differently.
- Rebalance band, scenario values and the volatility estimator are conventions.
- Phase 10 ML states can be fed to `RiskOverlay` but were deliberately not included.
