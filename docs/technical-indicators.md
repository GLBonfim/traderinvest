# Technical Indicator Engine (Phase 5)

> Indicator values are **descriptive features**. No value, level or crossing implies buy, sell,
> entry, exit or expected profitability. Whether any indicator carries information about future
> returns is for later out-of-sample validation.

Code: `app/indicators/` (`indicators.py` formulas, `engine.py`, `config.py`, `cli.py`) ·
engine version `1.0.0` · parameters in `IndicatorConfig`.

## Inputs and price convention

- Consecutive **closed** bars, UTC index, strictly increasing (same contract as the other engines).
- OHLC must be finite and > 0 (otherwise `ValueError`: validate data first).
- **All indicators use raw OHLC and raw volume** — the same series as the Candlestick and Price
  Action engines (ADR-0014). `adj_close` is not used and no corporate-action handling is done.
  Consequence: close-to-close returns (ROC, RSI, realized volatility, OBV direction) include the
  ex-dividend price drop (~0.3–0.5% per quarter for SPY).
- **Volume may be missing** (NaN): volume indicators become NaN (relative volume for the windows
  containing the gap; OBV from the gap onwards). Price indicators are unaffected. Negative or
  infinite volume is rejected. Volume is never invented.

## General conventions

| Topic | Convention |
|---|---|
| Rolling windows | trailing, full window required (`min_periods = n`), never centred |
| Recursive smoothing | seeded with the simple mean of the first n valid inputs; first output at that n-th input; leading NaNs skipped; a NaN after the first valid input raises (a recursion cannot be continued honestly across a gap) |
| EMA | α = 2 / (n + 1) |
| Wilder (RMA) | α = 1 / n |
| Undefined values | NaN (0/0, flat windows). Never replaced by 0, 50 or any other number |
| Well-defined limits | kept (RSI 100 / 0, zero band width, zero volatility) |
| Rounding | ratios/percentages rounded to 1e−10 (project convention for threshold stability) |
| Warm-up | values before the first valid index are NaN, never 0 |

Indices below are 0-based bar positions (index 19 = the 20th bar).

## Definitions

| Column | Family | Definition | First valid index (defaults) |
|---|---|---|---|
| `sma_n` (20, 50, 200) | trend | mean(close[t−n+1 … t]) | n − 1 (19, 49, 199) |
| `ema_n` (20, 50, 200) | trend | EMA[n−1] = mean(close[0 … n−1]); EMA[t] = α·close[t] + (1−α)·EMA[t−1], α = 2/(n+1) | n − 1 |
| `macd` | trend | EMA12(close) − EMA26(close) (each a standalone EMA as above) | 25 |
| `macd_signal` | trend | EMA9 of `macd`, seeded with the mean of the first 9 MACD values | 25 + 8 = 33 |
| `macd_hist` | trend | `macd − macd_signal` | 33 |
| `rsi_14` | momentum | Δ = close − prev close; G, L = Wilder14 of max(Δ,0), max(−Δ,0), seeded with the mean of Δ[1…14]; RSI = 100·G/(G+L) (≡ 100 − 100/(1+G/L)) | 14 |
| `stoch_raw_k` | momentum | 100·(close − LL14)/(HH14 − LL14), LL/HH = lowest low / highest high of the last 14 bars | 13 |
| `stoch_k` | momentum | SMA3(raw %K) ("slow" %K of a 14/3/3 stochastic) | 15 |
| `stoch_d` | momentum | SMA3(%K) | 17 |
| `roc_12` | momentum | 100·(close / close[t−12] − 1) | 12 |
| `true_range` | volatility | max(H − L, \|H − prev close\|, \|L − prev close\|); NaN on bar 0 (no previous close) | 1 |
| `atr_14` | volatility | Wilder14 of TR, seeded with mean(TR[1…14]) | 14 |
| `bb_middle` | volatility | SMA20(close) | 19 |
| `bb_upper` / `bb_lower` | volatility | middle ± 2.0 × std20(close), **ddof = 0** (population, Bollinger's convention) | 19 |
| `bb_width_pct` | volatility | (upper − lower) / middle | 19 |
| `bb_percent_b` | volatility | (close − lower) / (upper − lower) | 19 |
| `realized_vol_20` | volatility | std of the last 20 log returns ln(close[t]/close[t−1]), **ddof = 1** (sample), × √252 (trading days per year) | 20 |
| `relative_volume_20` | volume | volume[t] / mean(volume[t−20 … t−1]) — **current bar excluded** from the baseline (identical to the Candlestick Engine's `relative_volume`) | 20 |
| `obv` | volume | OBV[0] = 0; OBV[t] = OBV[t−1] + sign(close[t] − close[t−1])·volume[t]; unchanged close adds 0 | 0 |

The engine also returns a `catalog` (column, family, inputs, definition, first_valid_index);
tests assert that every column is NaN exactly before its catalogued index and never NaN after
it on valid data.

## Edge cases

| Situation | Behaviour |
|---|---|
| RSI with gains and no losses | 100 |
| RSI with losses and no gains | 0 |
| RSI with neither (flat) | NaN (undefined) |
| Stochastic with HH = LL | raw %K NaN; %K/%D NaN for windows containing it |
| Bollinger on flat prices | upper = lower = middle; `bb_width_pct` = 0; `bb_percent_b` NaN |
| Realized volatility of constant returns | 0 |
| Relative volume with zero baseline | NaN |
| Zero volume | relative volume 0 (if baseline > 0) or NaN; OBV adds 0 |
| Missing volume | relative volume NaN while the gap is in the window; OBV NaN from the gap onwards |
| Very small / very large prices | dimensionless indicators are scale-invariant (tested at ×1e−6 and ×1e6) |
| Missing/infinite/non-positive OHLC | rejected (`ValueError`) |

## Cross-validation against TA-Lib

`uv run --with TA-Lib python scripts/crossvalidate_indicators.py` (TA-Lib 0.8.1, ephemeral; not
a project dependency) on the 8,479 SPY bars:

| Indicator | Result |
|---|---|
| SMA 20/50/200, EMA 20/50/200, RSI, true range, ATR, Bollinger (3 bands), realized vol, ROC, %D | identical warm-up; max abs difference ≤ 3e−10 |
| `macd`, `macd_signal`, `macd_hist` | **initialisation difference**: TA-Lib seeds the fast EMA at the slow EMA's first bar and publishes all three outputs from index 33; we use standalone EMAs (MACD from index 25). Differences up to 0.13 early on vanish after warm-up: max abs difference after bar 300 = 5.7e−13 |
| `stoch_k` | values identical (4.8e−11); TA-Lib publishes %K only from index 17 (aligned with %D), we publish it when computable (index 15) |
| `obv` | constant offset = volume[0] (TA-Lib starts OBV at volume[0], we start at 0); the difference is constant over all bars |

Conventions were chosen from the definitions above, not to match any outcome. The script exits
non-zero if any unexplained difference appears.

## Point-in-time guarantees (tested)

- For every bar T, values computed with bars up to T equal the full-history values at T
  (synthetic 260 bars with default periods; real SPY 2020 excerpt with short periods).
- Randomly altering every bar after T (prices and volume) never changes any value at or before T.
- A deliberately centred (future-peeking) SMA is detected by the same check (control test).
- The caller's bar frame is never modified.

## Output

`IndicatorEngine().analyze(bars, instrument_id=..., timeframe=...)` → `IndicatorAnalysis`:
`values` (index = bar ts; `instrument_id`, `timeframe`, then indicator columns), `catalog`,
`engine_version`, `config_fingerprint`, `value_as_of(ts)`. Nothing is persisted (ADR-0010).
CLI: `uv run python -m app.indicators.cli latest --symbol SPY`.

Performance: ~28 ms median for 8,479 SPY bars (9 runs, local machine).

## Limitations

- Raw (dividend-unadjusted) closes: return-based indicators include ex-dividend drops.
- OBV is cumulative from the first available bar: its level depends on where the history
  starts; only its changes are comparable across datasets.
- MACD early values differ from TA-Lib's by construction (documented initialisation convention).
- Recursive indicators (EMA, MACD, RSI, ATR) depend on the whole history since their seed; two
  datasets with different start dates converge only after several multiples of the period.
- No missing-bar detection beyond Phase 2 validation: the engine assumes consecutive sessions.
