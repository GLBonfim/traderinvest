# Candlestick Engine (Phase 3)

> **Candlestick patterns are hypotheses/features, not validated trading edges.**
> The engine describes candle shapes. It produces no signals, no buy/sell decisions, no prices
> to act on and no expected returns. Whether any pattern carries information about future
> returns is an open question for later out-of-sample validation, and "no statistically
> significant edge" is an acceptable answer.

Code: `app/candles/` · Engine version `1.0.0` · thresholds in `app/candles/config.py`.

```
closed OHLCV bars ─► geometry (per bar) ─► 20 detectors ─► observations (long format)
```

## Input

- Consecutive **closed** bars of one instrument and timeframe (the engine raises if
  `is_closed` is present and any bar is not closed).
- Index: tz-aware, **UTC**, strictly increasing, unique. For daily bars `ts` is the session
  open (Phase 2 convention); observations inherit it.
- Prices: provider **raw OHLC** (`open/high/low/close`). `adj_close` is deliberately not used:
  adjusting only the close distorts body and wick geometry. For SPY (no splits) raw OHLC is
  as-traded. Loader: `app/candles/loader.py` (`is_closed = true` only, read-only).

## Geometry

With R = high − low:

| Feature | Definition |
|---|---|
| `range` | R |
| `body` | close − open (signed) |
| `abs_body` | \|close − open\| |
| `upper_wick` | high − max(open, close) |
| `lower_wick` | min(open, close) − low |
| `body_ratio` | abs_body / R |
| `upper_wick_ratio` | upper_wick / R |
| `lower_wick_ratio` | lower_wick / R |
| `close_position` | (close − low) / R (0 = at low, 1 = at high) |
| `direction` | bullish if close > open, bearish if close < open, neutral if equal, invalid for invalid rows |
| `gap` | open / previous close − 1 |
| `relative_volume` | volume / mean(volume of the previous 20 bars) |
| `avg_body_prior` | mean(abs_body of the previous 20 bars) |
| `trend_score` | (close[t−1] − close[t−11]) / mean(R of bars t−10 … t−1) |
| `prior_trend` | up if trend_score ≥ 1.0, down if ≤ −1.0, none otherwise, unknown without full history |

Rules:

- **Past-only.** Rolling statistics use bars strictly before t (`shift(1)` then rolling) and
  require a full window (otherwise NaN / `unknown`).
- **Zero-range candles** (R = 0): valid data, ratios NaN, never part of any pattern.
- **Invalid rows** (missing value, high < max(open, close), low > min(open, close), low ≤ 0,
  volume < 0): NaN geometry, `direction = invalid`, never part of any pattern; they also make
  rolling windows that contain them NaN.
- **Numerical stability:** ratios are rounded to 1e−10 so that a value mathematically equal to a
  threshold (0.2 / 2 = 0.1) is not lost to floating-point error (0.10000000000000142).
  Comparisons are inclusive (`≥`, `≤`) unless stated as strict.

## Notation for definitions

`br`, `uw`, `lw` = body, upper-wick, lower-wick ratios. Subscripts: `p` = previous candle,
`1..k` = candles of a k-candle pattern ending at t (k = t). All thresholds are the defaults in
`CandleConfig`.

**Context.** For patterns with a conventional prior trend, the trend is `prior_trend` measured
**before the first candle of the pattern** (so the pattern's own candles never define their
context). Two cases:

- *Trend defines identity* (hammer/hanging man, inverted hammer/shooting star share shapes):
  emitted **only** when the trend matches; `context_requirements_met` is always true.
- *Trend is context*: emitted whenever the shape matches; `context_requirements_met` tells
  whether the conventional trend was present. This allows later phases to test
  "with context" vs "without context" instead of assuming context matters.

## Pattern definitions

### Reversal

| Pattern | Bars | Orientation | Definition | Context |
|---|---|---|---|---|
| hammer | 1 | bullish | br ≤ 0.30 ∧ lw ≥ 0.60 ∧ uw ≤ 0.10 ∧ lower_wick ≥ 2.0 × abs_body | prior trend **down** (defines identity) |
| hanging_man | 1 | bearish | same shape as hammer | prior trend **up** (defines identity) |
| inverted_hammer | 1 | bullish | br ≤ 0.30 ∧ uw ≥ 0.60 ∧ lw ≤ 0.10 ∧ upper_wick ≥ 2.0 × abs_body | prior trend **down** (defines identity) |
| shooting_star | 1 | bearish | same shape as inverted hammer | prior trend **up** (defines identity) |
| bullish_engulfing | 2 | bullish | p bearish ∧ br_p > 0.10 ∧ t bullish ∧ open ≤ close_p ∧ close ≥ open_p ∧ abs_body > abs_body_p | prior trend down |
| bearish_engulfing | 2 | bearish | p bullish ∧ br_p > 0.10 ∧ t bearish ∧ open ≥ close_p ∧ close ≤ open_p ∧ abs_body > abs_body_p | prior trend up |
| morning_star | 3 | bullish | 1 bearish, br₁ ≥ 0.50 ∧ br₂ ≤ 0.30 ∧ max(open₂, close₂) ≤ close₁ ∧ 3 bullish, br₃ ≥ 0.50 ∧ (close₃ − close₁)/(open₁ − close₁) ≥ 0.50 | prior trend down |
| evening_star | 3 | bearish | 1 bullish, br₁ ≥ 0.50 ∧ br₂ ≤ 0.30 ∧ min(open₂, close₂) ≥ close₁ ∧ 3 bearish, br₃ ≥ 0.50 ∧ (close₁ − close₃)/(close₁ − open₁) ≥ 0.50 | prior trend up |
| tweezer_bottom | 2 | bullish | p bearish ∧ t bullish ∧ \|low − low_p\| / mean(R, R_p) ≤ 0.05 | prior trend down |
| tweezer_top | 2 | bearish | p bullish ∧ t bearish ∧ \|high − high_p\| / mean(R, R_p) ≤ 0.05 | prior trend up |

### Continuation

| Pattern | Bars | Orientation | Definition |
|---|---|---|---|
| three_white_soldiers | 3 | bullish | each candle bullish ∧ br ≥ 0.50 ∧ uw ≤ 0.25; for candles 2, 3: close > previous close ∧ open within previous body [min(o,c), max(o,c)] |
| three_black_crows | 3 | bearish | each candle bearish ∧ br ≥ 0.50 ∧ lw ≤ 0.25; for candles 2, 3: close < previous close ∧ open within previous body |
| rising_three_methods | 5 | bullish | 1 bullish, br₁ ≥ 0.50; candles 2–4: high ≤ high₁ ∧ low ≥ low₁ ∧ abs_body < abs_body₁; 5 bullish, br₅ ≥ 0.50 ∧ close₅ > close₁ |
| falling_three_methods | 5 | bearish | mirror: 1 bearish; inner candles contained in candle 1's range with smaller bodies; 5 bearish, br₅ ≥ 0.50 ∧ close₅ < close₁ |

No trend context is required (`context_requirements_met` = true).

### Structure

| Pattern | Bars | Definition |
|---|---|---|
| inside_bar | 2 | high < high_p ∧ low > low_p (strict) |
| outside_bar | 2 | high > high_p ∧ low < low_p (strict) |

### Indecision

| Pattern | Bars | Definition |
|---|---|---|
| doji | 1 | br ≤ 0.10 |
| spinning_top | 1 | 0.10 < br ≤ 0.30 ∧ uw ≥ 0.25 ∧ lw ≥ 0.25 |

### Momentum (orientation = candle direction)

| Pattern | Bars | Definition |
|---|---|---|
| marubozu | 1 | br ≥ 0.90 ∧ uw ≤ 0.05 ∧ lw ≤ 0.05 |
| long_body | 1 | br ≥ 0.60 ∧ abs_body ≥ 1.5 × avg_body_prior (needs 20 prior bars) |

A candle can carry several patterns (e.g. a hammer with a tiny body is also a doji; a strong
engulfing candle may also be a marubozu, long body and outside bar). No single label is forced.

### Deliberate simplifications

- Shooting star and evening/morning star do not require price gaps (overnight body gaps are rare
  in daily index ETFs); the star must sit beyond the first candle's close instead.
- Three methods do not require the inner candles to drift against the trend.
- Three white soldiers / black crows have no prior-trend requirement.

## Strength

`strength ∈ [0, 1]` measures **geometric quality only**: the mean, over each pattern's key
criteria, of how far the value goes past its threshold toward an ideal extreme:

- `ge(x, t, ideal) = clip((x − t) / (ideal − t), 0, 1)` for "x ≥ t"
- `le(x, t, ideal) = clip((t − x) / (t − ideal), 0, 1)` for "x ≤ t"

A pattern exactly at its thresholds has strength 0.

| Pattern | Strength criteria |
|---|---|
| doji | le(br, 0.10, 0) |
| spinning_top | mean(ge(uw, 0.25, 0.5), ge(lw, 0.25, 0.5)) |
| marubozu | ge(br, 0.90, 1) |
| long_body | mean(ge(abs_body/avg_body_prior, 1.5, 3.0), ge(br, 0.60, 1)) |
| hammer, hanging_man | mean(ge(lw, 0.60, 1), le(uw, 0.10, 0), le(br, 0.30, 0)) |
| inverted_hammer, shooting_star | mirror of the above |
| engulfing | clip(log(abs_body/abs_body_p) / log(10), 0, 1) — log scale because the multiple is heavy-tailed (SPY median ≈ 3×, p90 ≈ 8×; a linear cap at 3× saturated for most observations) |
| morning/evening star | mean(ge(br₁, 0.5, 1), le(br₂, 0.3, 0), ge(br₃, 0.5, 1), ge(penetration, 0.5, 1)) |
| tweezers | le(diff, 0.05, 0) |
| soldiers / crows | mean of ge(brᵢ, 0.5, 1) for the 3 candles |
| three methods | mean(ge(br₁, 0.5, 1), ge(br₅, 0.5, 1)) |
| inside_bar | le(R/R_p, 1, 0) |
| outside_bar | ge(R/R_p, 1, 3) |

Strength is **never** computed from future returns and carries no claim of predictive power.

## Output

`CandlestickEngine().analyze(bars, instrument_id=..., timeframe=...)` returns `CandleAnalysis`:

- `geometry`: one row per input bar (all features above, plus `valid`, `zero_range`, `usable`).
- `observations`: one row per (candle, pattern), sorted by (`ts`, `pattern`), columns
  `ts, instrument_id, timeframe, pattern, family, orientation, bars_in_pattern, first_bar_ts,
  strength, context_requirements_met, prior_trend, trend_score, body_ratio, upper_wick_ratio,
  lower_wick_ratio, range, relative_volume`. `ts` = open time of the pattern's **last** candle
  (the earliest moment the pattern is fully known once that candle closes).
- `engine_version`, `config_fingerprint` (hash of version + thresholds) for provenance.

CLI (descriptive counts only): `uv run python -m app.candles.cli scan --symbol SPY`.

## Point-in-time guarantees (tested)

- For every bar T, `analyze(bars[:T+1])` gives the same geometry and observations at T as
  `analyze(all bars)` (synthetic random walk and real SPY excerpt).
- Randomly altering every bar after T never changes any observation at or before T.
- A deliberately leaky feature (`shift(-1)`) is detected by the same check (control test).
- `first_bar_ts ≤ ts` and the window spans exactly `bars_in_pattern` bars.

## SPY daily occurrence counts (descriptive only)

8,479 closed bars (1993-01-29 → 2026-10-06), yfinance, engine 1.0.0, fingerprint
`6e878643a2dd9a7b`. Counts say how often shapes occur, **nothing** about outcomes.

| Pattern | n | context met | Pattern | n | context met |
|---|---|---|---|---|---|
| long_body | 1,689 | — | tweezer_top | 210 | 92 |
| inside_bar | 898 | — | tweezer_bottom | 158 | 36 |
| outside_bar | 858 | — | hanging_man | 134 | 134 |
| doji | 854 | — | hammer | 56 | 56 |
| spinning_top | 800 | — | shooting_star | 54 | 54 |
| bearish_engulfing | 295 | 136 | inverted_hammer | 43 | 43 |
| bullish_engulfing | 233 | 57 | evening_star | 38 | 10 |
| marubozu | 218 | — | morning_star | 31 | 10 |
| three_white_soldiers | 15 | — | three_black_crows | 7 | — |
| rising_three_methods | 3 | — | falling_three_methods | 2 | — |

Several patterns are rare on SPY daily (three methods: 2–3; crows: 7). Any later statistics on
them will almost certainly be `INSUFFICIENT DATA`.

Performance: ~126 ms median for all 8,479 bars (7 runs, local machine).

## Limitations

- Thresholds are conventions, not optimised; results may be sensitive to them.
- `prior_trend` is a simple volatility-normalised net move over 10 bars; other trend
  definitions would classify context differently.
- The engine assumes consecutive bars; Phase 2 found no missing SPY sessions, but a gap in the
  input would make "previous candle" refer to an earlier session.
- No persistence: observations are recomputed on demand (ADR-0010).
