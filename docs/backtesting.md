# Backtesting Engine (Phase 8)

> **A backtest is a historical simulation under explicit assumptions. It is not evidence that
> any strategy is profitable, superior or likely to make money.** No order is ever sent; no
> broker, paper broker or live execution exists. Statistical validation (out-of-sample,
> multiple-testing) belongs to Phase 9.

Code: `app/backtest/` (`config.py`, `execution.py`, `portfolio.py`, `metrics.py`, `engine.py`,
`models.py`, `data.py`, `cli.py`) · engine version `1.0.0`.

## Timing and execution convention

```
bar T closes ─► strategy observes T (observed_at = session close of T)
            ─► state decided at T
            ─► effective_at = next XNYS session open
            ─► execution at OPEN of bar T+1 (default and only model: next_session_open)
```

- Execution price = **raw open of the next session**. Close(T), high/low(T), and any intraday
  price of T+1 other than its open are never used.
- Every fill is validated by the engine: it must be after the decision bar, at the first session
  after it, equal to the strategy's `effective_at`, and priced at that session's open.
  Execution models violating any of these are rejected (tests: execution at close(T), at
  high(T+1), skipping a session).
- A missing session in the data is an error (the next bar must be the next exchange session).
- **Final bar:** a decision observed on the last bar has no next session in the data; it is
  reported as `pending_state` ("not_executed_no_next_session_in_data"), never executed.
- Timestamps are UTC; sessions (holidays, weekends, early closes) come from the XNYS calendar.

## Position model and capital

- Single instrument; position ∈ {0 = FLAT (all cash), 1 = LONG (all equity in the instrument)}.
- No shorting, leverage, margin or partial sizing. Fractional shares are allowed (research
  simplification so that "fully invested" is exact).
- Initial capital 100,000 (a normalised research base, not a recommendation). Returns are
  scale-free except for the flat commission per order.
- An order happens only when the target position changes (LONG→LONG or FLAT→FLAT: none).

### INSUFFICIENT_DATA policy

`INSUFFICIENT_DATA` never opens or closes a position: the target stays what it was (0 before
the first valid state). Only transitions between valid LONG/FLAT states trade.

## Cost assumptions (research defaults, not broker quotes)

Per order (each entry and each exit is one order), on its notional N:

| Component | Default | Formula |
|---|---|---|
| commission | 1.00 per order | max(commission_per_trade + N·commission_bps/1e4, minimum_commission) |
| spread | 2 bps full quoted spread | N · spread_bps/2 / 1e4 (half paid per order) |
| slippage | 2 bps per order | N · slippage_bps / 1e4 |

Default total ≈ 3 bps of notional + 1.00 per order. These values are generic, deliberately
non-zero assumptions; they were **not** calibrated on any SPY result. A buy spends all cash:
N + costs(N) = cash. Costs are separate amounts; fills are recorded at the raw open.

**Gross vs net:** the engine runs the same states twice — with costs (net, used for the equity
curve) and with zero costs (gross) — so `gross_cumulative_return − cumulative_return`
(`cost_drag_cumulative`) shows how much the assumptions consume. Every trade exposes gross P&L,
commission, spread cost, slippage cost, total cost and net P&L.

## Raw vs adjusted data

- Strategy features: unchanged raw-OHLC definitions (Phases 3–7).
- Execution and strategy P&L: raw prices. **Timing strategies therefore earn price return only;
  dividends are not credited while LONG.**
- Benchmarks:
  - `benchmark_buy_and_hold_price`: buy & hold valued with raw prices (price return).
  - `benchmark_buy_and_hold_total`: identical rule and costs, valued with the provider's
    adjusted series: open × adj_close/close and adj_close (dividends reinvested as reflected
    by the adjustment). Adjusted data is used **only** here; absent if `adj_close` is missing.
- Fair comparison of a timing strategy is against the **price** benchmark; the total-return
  benchmark shows what dividends add.

## Returns and equity curve

Per session t of the window (flat days included):

- `equity_t = cash_t + shares_t × close_t` (mark to market at the close)
- `daily_return_t = equity_t / equity_{t−1} − 1`, with equity before the first session = initial
  capital; cumulative return = equity_N / initial − 1 = Π(1 + r) − 1 (tested)
- gross counterparts from the zero-cost run
- equity columns: `state, state_in_effect, position, shares, open, close, executed,
  traded_notional, costs, cash, equity, daily_return, cumulative_return, running_peak,
  drawdown, gross_equity, gross_daily_return, gross_cumulative_return`

## Metrics (end-of-sample, descriptive; never used for positions)

A = 252 sessions/year (stated annualisation). N = sessions in the window.

| Metric | Definition | Undefined (NaN) when |
|---|---|---|
| cumulative return | equity_N / initial − 1 | no sessions |
| CAGR | (1 + cumulative)^(A/N) − 1 | N = 0 or final equity ≤ 0 |
| annualised volatility | std(r, ddof = 1) · √A | N < 2 |
| Sharpe | mean(r − rf) / std(r − rf, ddof = 1) · √A, rf = 0 (research convention) | N < 2 or std = 0 |
| Sortino | mean(r − rf) / √mean(min(r − rf, 0)²) · √A (target 0, downside over all N) | N < 2 or no negative return |
| drawdown | equity_t / max(initial, equity_0…t) − 1 | — |
| max drawdown | min(drawdown) | — |
| max drawdown duration | longest run of consecutive sessions with drawdown < 0 | — |
| completed trades, win rate, avg / median / best / worst trade net return, avg holding sessions | completed trades only | no completed trade |
| exposure | share of sessions with an open position at the close | — |
| turnover | Σ traded notional / mean equity; annualised × A/N | — |
| top-5 winners' share of gross profit | Σ net P&L of the 5 best trades / Σ net P&L of all winning trades | no winning trade |

Open position at the end: marked to market at the last close (`open_position`, status
`open_marked_to_market`), with unrealized P&L net of entry costs only (exit costs not applied).
`realized_pnl + unrealized_pnl = total_pnl` (tested).

## Temporal slicing

`BacktestConfig(start, end, period_label)`: strategies are computed on bars **≤ end only**
(nothing later is read — tested by mutating data after `end`); the backtest window is
[start, end] with fresh capital; a state decided before `start` executes at the window's first
open. `period_label` (e.g. train/validation/test) is metadata only — a date range does not
make anything "out-of-sample".

## Point-in-time guarantees (tested)

- Backtest on bars ≤ T vs full history: equity curve, fills and completed trades through T are
  identical (every 6th bar of a 150-session series, all strategies and both benchmarks).
- Mutating OHLC, volume and `adj_close` after T never changes anything through T.
- Drawdown series and max drawdown through T equal those of the backtest ending at T.
- Controls: execution at close(T), at high(T+1), skipping a session, a missing session — all
  rejected; a rule using tomorrow's close is detected by the prefix check.

## Baseline diagnostic — SPY daily, 8,479 sessions (1993-01-29 → 2026-10-06), default assumptions

**Descriptive only. Single historical path, single instrument, no statistical testing, fixed
textbook parameters. Not evidence of future performance; no strategy is selected.**

| Strategy | Net cum. return | Gross cum. return | Cost drag | CAGR | Ann. vol | Sharpe (net) | Sortino | Max DD | Max DD duration |
|---|---|---|---|---|---|---|---|---|---|
| buy_and_hold | 16.71 | 16.72 | 0.006 | 8.92% | 18.6% | 0.55 | 0.78 | −56.5% | 1,804 |
| sma_trend | 7.65 | 8.26 | 0.61 | 6.62% | 11.8% | 0.60 | 0.83 | −30.0% | 1,960 |
| sma_crossover | 3.40 | 3.64 | 0.25 | 4.50% | 11.7% | 0.44 | 0.60 | −37.6% | 3,676 |
| rsi_momentum | 1.47 | 2.31 | 0.83 | 2.73% | 10.9% | 0.30 | 0.42 | −40.0% | 3,177 |
| price_action_trend | 0.78 | 0.85 | 0.07 | 1.73% | 6.4% | 0.30 | 0.42 | −24.4% | 2,184 |
| regime_trend | 0.77 | 0.84 | 0.06 | 1.72% | 6.4% | 0.30 | 0.41 | −24.4% | 2,184 |
| benchmark price | 16.71 | 16.72 | 0.006 | 8.92% | 18.6% | 0.55 | 0.78 | −56.5% | 1,804 |
| benchmark total | 31.36 | 31.37 | 0.010 | 10.89% | 18.5% | 0.65 | 0.93 | −55.2% | 1,656 |

(Cumulative returns are multiples − 1, e.g. 16.71 = +1,671%.)

| Strategy | Completed trades | Orders | Win rate | Median trade | Avg holding (sessions) | Exposure | Ann. turnover | Total costs | Top-5 share of profit |
|---|---|---|---|---|---|---|---|---|---|
| buy_and_hold | 0 (open) | 1 | — | — | — | 99.99% | 0.006 | 31 | — |
| sma_trend | 111 | 223 | 30.6% | −0.83% | 54.8 | 73.2% | 6.0 | 19,454 | 58.4% |
| sma_crossover | 89 | 179 | 50.6% | +0.24% | 61.7 | 66.1% | 5.0 | 10,570 | 38.9% |
| rsi_momentum | 473 | 947 | 33.0% | −0.57% | 11.8 | 66.1% | 27.9 | 42,768 | 17.2% |
| price_action_trend | 60 | 120 | 58.3% | +0.46% | 27.5 | 19.4% | 3.6 | 5,642 | 39.6% |
| regime_trend | 58 | 116 | 58.6% | +0.46% | 28.3 | 19.3% | 3.5 | 5,493 | 39.9% |

Observations (not conclusions):

- **Execution timing:** every fill is at a real session open strictly after its signal (verified
  on all 8,479 sessions).
- **Costs:** `rsi_momentum` has the highest turnover (≈28× per year, 947 orders); its gross
  cumulative return 2.31 falls to 1.47 net under the default assumptions — the largest cost drag.
- **Exposure:** the price-action and regime baselines are invested ~19% of the time; the SMA
  baselines 66–73%.
- **Redundancy:** `price_action_trend` and `regime_trend` hold the same position on 99.91% of
  sessions (daily-return correlation 0.998).
- **Benchmarks:** price-return vs total-return buy & hold differ substantially (16.7 vs 31.4),
  i.e. dividends matter over this horizon; timing strategies are not credited dividends.
- **Concentration:** for `sma_trend` the 5 best trades account for 58% of gross winning P&L.
- Drawdowns of all timing baselines are shallower than buy & hold, together with lower exposure;
  this is a description of this path, not a risk claim.

Performance (median of 5, local machine): backtest-only ~0.15–0.22 s per strategy; six
strategies ~1.1 s; both benchmarks ~0.46 s; full suite (strategy features + backtests +
benchmarks) ~4.9 s, dominated by the Price Action Engine (computed twice).

## Limitations

- One instrument, one path, daily bars, fixed parameters: no inference is possible here.
- Costs are generic assumptions; real spreads/slippage vary over time (much wider in 1993).
- Opening-auction fills at the exact open price are assumed; no partial fills, no market impact.
- Timing strategies earn no dividends (price return only).
- Fractional shares; no taxes, borrowing, cash interest (rf = 0).
- Survivorship/selection: SPY was chosen in advance; the six rules are textbook conventions,
  but the decision to study them is not independent of general market knowledge.
