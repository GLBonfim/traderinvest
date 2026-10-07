# Baseline Strategies (Phase 7)

> **Baseline strategies are research benchmarks, not evidence of profitability.** They are
> fixed, textbook rules used to measure, in Phase 8, how each family of information behaves on
> its own. Nothing here has been evaluated, optimised or shown to work.

Code: `app/strategies/` (`base.py` contract, `baselines.py` rules, `engine.py`, `config.py`,
`cli.py`) · engine version `1.0.0` · parameters in `StrategyConfig`.

## Observation → Rule → State

```
Phases 3–6: observations (indicators, structure, regimes) at bar T
        ↓  fixed rule (this phase), reading ONLY its declared features
state at T: LONG / FLAT / INSUFFICIENT_DATA   ← decided at the close of T
        ↓  (Phase 8 decides how a state becomes a hypothetical trade, with explicit costs)
```

No orders, prices, fills, sizes, stops, targets, expected returns, probabilities, scores or
confidence values are produced. There is no SHORT state.

## States

| State | Meaning |
|---|---|
| `LONG` | the rule's condition holds at T |
| `FLAT` | the rule's inputs are all known at T and the condition does not hold |
| `INSUFFICIENT_DATA` | at least one input is missing at T (warm-up or undefined value) — **never** converted to FLAT |

## Timing convention (ADR-0016)

| Field | Definition |
|---|---|
| `bar_ts` | session open of bar T (Phase 2 convention) |
| `observed_at` | session **close** of T from the XNYS calendar — when every feature of T is known and the state is decided (signal timestamp) |
| `effective_at` | **open of the next XNYS session** — the earliest moment the state may apply |
| `state_in_effect` | the state that applies during bar T = the state decided at T−1 (`INSUFFICIENT_DATA` on the first bar) |

`effective_at` comes from the exchange calendar, which is known in advance, so it does not
depend on whether a later bar exists in the data (prefix-safe). Early closes and holidays are
respected (e.g. 2024-07-03 is observed at 17:00 UTC and effective 2024-07-05 13:30 UTC). No
execution price is attached; whether a state is filled at the next open or otherwise is a
Phase 8 assumption.

## The six baselines

| Id | Features (only these are passed to the rule) | Rule at T | Warm-up (first decision, 0-based bar) |
|---|---|---|---|
| `buy_and_hold` | `close` | LONG whenever a close exists | 0 |
| `sma_trend` | `close`, `sma_200` | LONG if close > SMA200; FLAT if close ≤ SMA200 | 199 |
| `sma_crossover` | `sma_20`, `sma_50` | LONG if SMA20 > SMA50; FLAT if SMA20 ≤ SMA50 | 49 |
| `rsi_momentum` | `rsi_14` | LONG if RSI14 > 50; FLAT if RSI14 ≤ 50 | 14 |
| `price_action_trend` | `pa_structure` | LONG if Price Action structure = `uptrend`; FLAT for `downtrend`/`range`/`transition` | Price Action warm-up (51 on SPY) |
| `regime_trend` | `regime_composite` | LONG if composite regime = `trending_up`; FLAT for every other known label | regime warm-up (272 on SPY) |

`buy_and_hold` is the **benchmark**, not a timing rule; it follows the same timing convention
(decided at the first close, in effect from the next session).

Equality goes to FLAT (close = SMA, SMA20 = SMA50, RSI = 50). An RSI that is undefined after
warm-up (flat prices) gives INSUFFICIENT_DATA, not FLAT. Price Action states inherit the 5-bar
pivot confirmation latency: a LONG can only begin on a bar where a pivot is confirmed.

Features come from the existing engines (no formula re-implemented): SMA/RSI from the Indicator
Engine, `pa_structure` from the Price Action Engine, `regime_composite` from the Regime Engine;
`close` is the raw close. Only the engines required by the selected strategies are run.

## Signal record

`StrategyEngine().run(bars, instrument_id=...)` → `StrategyRun`:

- `signals` (one row per strategy and bar): `strategy_id, strategy_version, instrument_id,
  timeframe, bar_ts, observed_at, effective_at, state, changed, state_in_effect, reason,
  config_fingerprint`. `changed` = state differs from the previous bar's; `reason` records the
  exact feature values the rule saw (e.g. `close=46.375000;sma_200=45.205937`).
- `summary` (per strategy): state counts, number of state changes, first decision bar.
- `features`: the feature frame the rules were applied to.
- `config_fingerprint` covers the strategy config, all upstream engine configs and the
  strategy ids/versions/features.

CLI (states only, no performance): `uv run python -m app.strategies.cli run --symbol SPY`.

## Guarantees (tested)

- **Prefix/full history**: every strategy row at T is identical whether computed on bars ≤ T or
  on the full history (all 160 bars of a session-indexed synthetic series).
- **Future mutation**: altering OHLC and volume after T changes nothing ≤ T.
- **Control**: a deliberately leaky rule (tomorrow's close vs today's SMA) is detected.
- **Execution latency**: `state_in_effect(T) = state(T−1)`; a flip decided at T is not in
  effect at T; `effective_at(T)` is the next bar's session.
- **Price Action latency**: LONG starts only on pivot-confirmation bars; the pivot bar itself
  does not show the state its later confirmation creates.
- **Isolation**: each rule receives only its declared columns (reading another column raises);
  scrambling every undeclared feature, or volume, never changes a state.

## SPY daily state counts (descriptive, 8,479 bars, default configuration)

| Strategy | LONG | FLAT | INSUFFICIENT_DATA | state changes |
|---|---|---|---|---|
| buy_and_hold | 8,479 | 0 | 0 | 0 |
| sma_trend | 6,209 | 2,071 | 199 | 223 |
| sma_crossover | 5,606 | 2,824 | 49 | 179 |
| rsi_momentum | 5,603 | 2,862 | 14 | 948 |
| price_action_trend | 1,647 | 6,781 | 51 | 121 |
| regime_trend | 1,639 | 6,568 | 272 | 117 |

These are counts of states, not results. Nothing about returns is known or implied.

Performance (median of 5 runs, 8,479 bars): rule-only strategies 86–120 ms each;
`price_action_trend` ~1.5 s and `regime_trend` ~1.6 s (dominated by their upstream engines);
all six ~3.2 s (Price Action is computed twice: once directly and once inside the Regime
Engine).

## Limitations

- Fixed textbook parameters; no claim that they are good choices.
- Daily bars and the XNYS calendar only.
- `regime_trend` and `price_action_trend` are nearly identical on SPY because the composite's
  `trending_up` is the Price Action uptrend after the 272-bar volatility warm-up.
- RSI momentum changes state often (948 changes), which will matter once costs exist (Phase 8).
- Duplicate Price Action computation when both trend-based baselines run (performance only).
