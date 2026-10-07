# Price Action Engine (Phase 4)

> The Price Action Engine describes **what price is doing across candles**: swings, structure,
> zones, events. It produces **observations** and, separately, later **outcomes**. It never
> produces **trading decisions**.

| Layer | Meaning | Known when | Example |
|---|---|---|---|
| OBSERVATION | Something that happened, measurable at its `available_at` | at that bar's close | "close crossed above the 110 zone" |
| OUTCOME | What happened afterwards to an observation | later, at `outcome_at` | "price closed back below 110 three bars later" |
| TRADING DECISION | Buy/sell/size | **not implemented** | — |

Code: `app/price_action/` · engine version `1.0.0` · thresholds in
`app/price_action/config.py` (`PriceActionConfig`). The Candlestick Engine is reused for wick
ratios and candle direction (no duplicated formulas); the two engines stay separate.

## Inputs

Consecutive **closed** bars (UTC index, strictly increasing, no missing OHLCV), e.g. from
`app.candles.loader.load_closed_bars`. Timeframe-agnostic; only SPY daily is used in Phase 4.

## Point-in-time semantics

Every bar `t` is processed in order:

1. **Events at t** (breakout, breakdown, rejection, sweep) are evaluated against zones available
   at the close of `t−1`.
2. **Pending breaks** from earlier bars are updated: retest observations and outcomes.
3. **Pivots confirmed at t** are added (labels, structure, zones).
4. **Touches** of zones available before `t` are recorded.
5. The **state row for t** is emitted.

No step reads a bar after `t`. Hence `state(t)`, and every swing/event/outcome with
`available_at`/`outcome_at ≤ t`, are identical whether or not later bars exist. Output dtypes
are explicit so that even the *representation* of `state(t)` cannot depend on later rows.

| Object | Timestamps |
|---|---|
| swing | `pivot_ts` (where the extreme is) · `confirmed_at` = `available_at` (when it is known) |
| zone | `first_pivot_ts` · `first_seen` (confirmation of its first pivot) · `last_tested` |
| event | `ts` = `available_at` (close of the bar) |
| outcome | `event_ts` · `outcome_at` (when the outcome became known) |
| state row | index = bar `ts`; describes the market after that bar closed |

`PriceActionAnalysis.state_as_of(ts)` returns the state after the last bar with index ≤ `ts` —
the hook for future multi-timeframe alignment. Bar `ts` is the bar **open** (Phase 2
convention); a higher-timeframe bar may only be used once it has **closed**, which the caller
must respect when aligning timeframes (see Limitations).

## 1. Swings and confirmation latency

With `k_l = swing_left_bars = 5`, `k_r = swing_right_bars = 5`:

- swing high at t: `high[t] > max(high[t−k_l … t−1])` **and** `high[t] ≥ max(high[t+1 … t+k_r])`
- swing low at t: `low[t] < min(low[t−k_l … t−1])` **and** `low[t] ≤ min(low[t+1 … t+k_r])`

Strict on the left and inclusive on the right: among equal highs/lows the **first** one is the
pivot. A pivot at t needs `k_r` future bars, so it is **confirmed at `t + k_r`**
(`confirmed_at`). A candidate without `k_r` later bars is not reported. With the defaults every
pivot is known 5 bars after its extreme; structure, zones and events only use confirmed pivots.

## 2. Structure labels (HH / HL / LH / LL / EH / EL)

Each pivot is compared with the previous pivot of the same kind:
`change = price / previous − 1`, tolerance `equal_tolerance_pct = 0.1%`.

| Kind | change > tol | change < −tol | \|change\| ≤ tol |
|---|---|---|---|
| high | HH | LH | EH (equal high) |
| low | HL | LL | EL (equal low) |

The first pivot of each kind has no label. Labels are structural observations only; they do
not mean buy or sell.

## 3. Structure classification and trend

Using the last `n = structure_min_labels = 2` high labels **and** the last 2 low labels known at t:

| Structure | Rule | Trend |
|---|---|---|
| insufficient_data | fewer than 2 high labels or 2 low labels | insufficient_data |
| uptrend | all highs HH **and** all lows HL | up |
| downtrend | all highs LH **and** all lows LL | down |
| range | highs ⊆ {LH, EH} **and** lows ⊆ {HL, EL} (flat or contracting) | sideways |
| transition | anything else (mixed, broadening HH+LL, …) | unclear |

`trend_quality` = share of the last `trend_quality_labels = 6` labels (highs and lows, in
confirmation order) consistent with the trend: up → {HH, HL}; down → {LH, LL}; sideways →
{LH, EH, HL, EL}; NaN for unclear/insufficient. `trend_evidence` lists those labels.
Quality measures **structural consistency only**, not a probability of future returns. No
moving averages are used (Phase 5).

## 4. Support / resistance zones

Built incrementally from confirmed pivots (`zone_tolerance_pct = 0.5%`,
`zone_max_width_pct = 2%`):

1. A pivot at price p joins the closest active zone whose bounds widened by `tol = p × 0.5%`
   contain p (tie → older zone), unless the zone would exceed 2% of its midpoint; otherwise it
   starts a new zone `[p, p]`.
2. After a zone grows, overlapping zones (within `mid × 0.5%`) are merged into the older one if
   the union respects the 2% cap.
3. A zone is **available from `first_seen`** = confirmation bar of its first pivot.
4. A touch = a later bar whose `[low, high]` intersects the zone (`touch_count`, `last_tested`).

Output: `zone_low, zone_high, zone_mid, width_pct, evidence_count` (pivots), `source_type`
(`swing_high` / `swing_low` / `mixed`), `first_pivot_ts, first_seen, last_tested, touch_count`.
A zone's role (support/resistance) is not stored: per bar, the state reports the nearest
available zone **below** the close (`support_*`), **above** it (`resistance_*`), and zones
containing the close (`inside_zone_ids`). Zones are observed historical price areas; nothing
claims they "will hold". Zones do not expire in v1.0.0.

## 5. Breakout / breakdown (close-based)

For each zone available at `t−1` (distances rounded to 1e−10; thresholds inclusive):

- **breakout**: `close[t−1] ≤ zone_high` **and** `close[t] / zone_high − 1 ≥ 0.1%`
- **breakdown**: `close[t−1] ≥ zone_low` **and** `close[t] / zone_low − 1 ≤ −0.1%`

Fields: `reference_low/high`, `reference_level` (the broken bound), `distance_pct`,
`confirmation_type = close`. A wick beyond the zone that closes back is **not** a breakout (see
sweep). One bar can break several zones (one event per zone). A breakout is an observation,
not a successful trade.

## 6. Retest

For a breakout at bar b with level L, the first bar j with `1 ≤ j − b ≤ retest_window_bars (10)`
where `low[j] / L − 1 ≤ 0.2%` (breakdown: `high[j] / L − 1 ≥ −0.2%`) emits a **retest**
observation at j (`related_event_id` = the break). Checked only while the break has no outcome
yet; a bar that closes back through the level is a failure outcome, not a retest. So
"breakout → retest" and "breakout → immediate reversal" are distinguished without rewriting
the breakout.

## 7. Rejection

At bar t, for a zone available at `t−1`:

- **from resistance** (zone above: `zone_low > close[t−1]`): `high[t] ≥ zone_low`, `close[t] < zone_low`,
  `upper_wick_ratio ≥ 0.5`, `(high − close) / close ≥ 0.3%`
- **from support** (zone below: `zone_high < close[t−1]`): `low[t] ≤ zone_high`, `close[t] > zone_high`,
  `lower_wick_ratio ≥ 0.5`, `(close − low) / close ≥ 0.3%`

Metrics: `wick_ratio`, `distance_pct`. No subjective "strong rejection".

## 8. Failed / held breakouts: outcomes, never observations

For each break at b with level L, scanning bars `j > b`:

- **failed** at the first j with `close[j] < L` (breakdown: `close[j] > L`), `j − b ≤ 10`
- **held** if no failure by `j − b = outcome_window_bars (10)`
- **pending** while neither is known (end of data)

Outcomes live in a separate table (`outcomes`) with `outcome_at` and `bars_to_outcome ≥ 1`.
The event row never contains its outcome; at the event's own bar the outcome is always unknown
(tested). Statistical validation may use outcomes only as of `outcome_at`.

## 9. Consolidation (range)

Window `W = range_window_bars = 20`: `window_width_pct = (max high − min low) / midpoint`.
`in_consolidation = window_width_pct ≤ 5%`. An episode is a run of consecutive consolidation
bars; `range_start` = first bar of the window of the episode's first bar; `range_high/low`
= extremes from `range_start` to t; `range_width`, `range_width_pct`, `range_bar_count`.
Price-normalised only (no ATR until Phase 5).

## 10. Expansion / contraction

- `candle_range_ratio = range[t] / mean(range[t−20 … t−1])`
- `window_width_ratio = window_width_pct[t] / window_width_pct[t−20]`
- state: expansion if ≥ 1.5, contraction if ≤ 0.5, normal otherwise, unknown without history.

Magnitude only. Direction is separate (`candle_direction`, `window_direction` = sign of
`close[t] − close[t−20]`) and is not a bullish/bearish judgement.

## 11. Sweep (objective liquidity-sweep-style event)

For a zone available at `t−1`:

- **up**: `close[t−1] ≤ zone_high`, `high[t] / zone_high − 1 ≥ 0.1%`, `close[t] ≤ zone_high`
- **down**: `close[t−1] ≥ zone_low`, `low[t] / zone_low − 1 ≤ −0.1%`, `close[t] ≥ zone_low`

i.e. price trades beyond a confirmed zone and closes back on its original side. Named
`sweep` as a purely geometric OHLCV event. **No claim** is made about institutional orders,
stops or liquidity: daily OHLCV contains no order-book information.

## Output (`PriceActionAnalysis`)

`swings`, `state` (one row per bar), `zones` (as of the last bar — for zones as of an earlier T,
analyse `bars.loc[:T]`), `events`, `outcomes`, `engine_version`, `config_fingerprint`.
CLI: `uv run python -m app.price_action.cli scan --symbol SPY` (descriptive counts).

## SPY daily, descriptive (8,479 bars, engine 1.0.0, fingerprint `adda5464db656be0`)

| Item | Count |
|---|---|
| confirmed swings | 996 |
| zones (active at the end) | 205 (max width 1.99%) |
| breakouts / breakdowns | 1,813 / 1,672 |
| retests up / down | 861 / 734 |
| rejections up / down | 539 / 288 |
| sweeps up / down | 1,019 / 1,375 |
| structure bars | transition 6,186 · uptrend 1,647 · downtrend 409 · range 186 · insufficient 51 |
| consolidation bars | 3,286 |

Outcome counts exist in the data but are **not** reported here as evidence of anything; their
analysis belongs to statistical validation.

Performance: ~1.36 s median for 8,479 bars (5 runs, local machine).

## Limitations

- Thresholds and rules are conventions. The strict 2-label structure rule classifies ~73% of SPY
  daily bars as `transition`.
- Zones never expire, so long histories accumulate zones; one bar can generate several events
  (one per zone), which inflates event counts and correlates them.
- Breakout/retest/outcome reference the zone bound **at event time**; later zone growth does
  not change past events.
- Multi-timeframe: interfaces are timeframe-agnostic and `state_as_of` exists, but bar `ts` is
  the bar open; aligning higher timeframes will need the bar close time (pending decision).
- Pivots use highs/lows only; no volume or candle-pattern conditions.
