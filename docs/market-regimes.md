# Market Regime Engine (Phase 6)

> **Regime labels describe observed market conditions and are not trading signals or
> predictions.** "uptrend", "high volatility" or "positive momentum" say what the market looked
> like at T using data available at T. They imply no direction to trade, no expected return and
> nothing about profitability.

Code: `app/regimes/` (`regimes.py` pure rules, `engine.py`, `config.py`, `cli.py`) · engine
version `1.0.0` · parameters in `RegimeConfig`.

## Composition (no duplicated formulas)

| Dimension | Source | Upstream guarantee |
|---|---|---|
| trend | `PriceActionEngine` → `structure` | pivots used only from `confirmed_at` |
| volatility | `IndicatorEngine` → `realized_vol_20` (+ `atr_14` exposed as `atr_pct`) | trailing windows |
| momentum | `IndicatorEngine` → `rsi_14`, `roc_12`, `macd` | trailing / recursive, causal |
| participation | `IndicatorEngine` → `relative_volume_20` | baseline excludes the current bar |

The Price Action Engine requires a complete volume column although none of its outputs depend
on volume (tested: all five Price Action outputs are identical with volume replaced by 0).
Missing volume is therefore passed to it as 0; that value never reaches any regime output.

## A. Trend regime

`trend_regime` = Price Action `structure` (`uptrend`, `downtrend`, `range`, `transition`,
`insufficient_data`), with `trend_quality` alongside. Rules are those of
[price-action-engine.md](price-action-engine.md) §3. A trend label can only change on a bar
where a pivot is confirmed (tested), so the 5-bar pivot confirmation latency is inherited.

## B. Volatility regime

Measure: `realized_vol_20` (std of 20 log returns, ddof 1, ×√252), from the Indicator Engine.

**Causal percentile** (`causal_percentile_rank`):

- reference(T) = valid measure values at bars **T−756 … T−1** (T itself **excluded**, no future)
- requires ≥ **252** valid reference values (otherwise `insufficient_data`)
- percentile(T) = (#{reference < x_T} + 0.5 · #{reference = x_T}) / #reference — mid-rank,
  **no interpolation**; a constant series has percentile 0.5
- values are compared after rounding to 1e−10, so floating-point noise (e.g. a mathematically
  zero volatility computed as 1e−17) is not ranked as a real difference
- `volatility_reference_count` reports the reference size

| Label | Rule |
|---|---|
| low | percentile < 0.20 |
| normal | 0.20 ≤ percentile < 0.80 |
| high | 0.80 ≤ percentile < 0.95 |
| extreme | percentile ≥ 0.95 |
| insufficient_data | measure missing or < 252 references |

Warm-up: realized volatility exists from bar 20; 252 references are reached at **bar 272**.
"High" therefore means high **relative to the previous ~3 years**, not in absolute terms. The
ATR-based `atr_pct = ATR14 / close` is exposed for context but not used in the label.

## C. Momentum regime

Inputs: RSI14, ROC12, MACD line (EMA12 − EMA26).

| Label | Rule |
|---|---|
| extreme_positive | positive **and** RSI ≥ 70 |
| positive | RSI > 50 **and** ROC > 0 **and** MACD > 0 |
| extreme_negative | negative **and** RSI ≤ 30 |
| negative | RSI < 50 **and** ROC < 0 **and** MACD < 0 |
| neutral | any disagreement, or any component exactly at its midpoint (RSI 50, ROC 0, MACD 0) |
| insufficient_data | any input missing |

Unanimity is required so that one oscillator alone cannot set the state; an RSI of 80 with a
negative ROC is `neutral`, not extreme. Warm-up: the MACD line (bar 25) is the binding input.
Momentum is a description of recent price change, not a recommendation.

## D. Participation (instrument-level volume — NOT breadth)

`participation_regime` from relative volume (volume ÷ mean of the previous 20 volumes): `low` if
< 0.5, `high` if ≥ 1.5, `normal` otherwise, `insufficient_data` when undefined (missing volume,
zero baseline, warm-up). This is SPY's own volume. **Market breadth is not implemented**: there
is no point-in-time S&P 500 constituent universe yet, and SPY volume is not breadth.

## Composite regime

A transparent table over trend × volatility only (momentum and participation stay separate to
avoid an arbitrary scoring model):

| trend \ volatility | low | normal | high | extreme | insufficient_data |
|---|---|---|---|---|---|
| uptrend | trending_up | trending_up | trending_up | trending_up | insufficient_data |
| downtrend | trending_down | trending_down | trending_down | trending_down | insufficient_data |
| range | ranging | ranging | ranging | ranging | insufficient_data |
| transition | low_volatility_transition | transition | high_volatility_transition | high_volatility_transition | insufficient_data |
| insufficient_data | insufficient_data | … | … | … | insufficient_data |

Volatility only qualifies `transition` states, where structure alone says little.

## Transitions and age

For each dimension (`trend`, `volatility`, `momentum`, `participation`, `composite`):

- `<dim>_changed(T)` = label(T) ≠ label(T−1); False on the first bar
- `<dim>_age(T)` = consecutive bars, including T, with the same label

No smoothing, hysteresis or persistence rule is applied: labels are the raw classification at
each bar. Both fields are known at T and are never rewritten by later data (tested).

## Output

`RegimeEngine().analyze(bars, instrument_id=..., timeframe=...)` → `RegimeAnalysis` with
`state` (one row per bar: labels, their inputs, `*_changed`, `*_age`), `engine_version`,
`config_fingerprint` (hash of the regime, Price Action and Indicator configurations), and
`state_as_of(ts)`. Nothing is persisted (ADR-0010).
CLI: `uv run python -m app.regimes.cli describe --symbol SPY`.

## Point-in-time guarantees (tested)

- For every bar T of a 220-bar series, the state at T from bars ≤ T equals the full-history
  state at T (all labels, inputs, flags and ages).
- Randomly altering every bar after T (prices and volume) never changes any state ≤ T.
- Control: replacing the causal percentile by a full-sample rank is detected.
- Trend labels change only on pivot-confirmation bars.
- Inputs are never modified; missing volume only affects participation.

## SPY daily, descriptive (8,479 bars, engine 1.0.0, default configuration)

| Dimension | Counts |
|---|---|
| trend | transition 6,186 · uptrend 1,647 · downtrend 409 · range 186 · insufficient 51 |
| volatility | normal 4,282 · low 1,903 · high 1,408 · extreme 614 · insufficient 272 |
| momentum | positive 3,778 · neutral 2,228 · negative 1,731 · extreme_positive 595 · extreme_negative 122 · insufficient 25 |
| participation | normal 7,150 · high 891 · low 418 · insufficient 20 |
| composite | transition 3,102 · trending_up 1,639 · high_vol_transition 1,629 · low_vol_transition 1,263 · trending_down 388 · insufficient 272 · ranging 186 |

Counts are descriptive; no threshold was chosen from them or from any return. The `extreme`
share (7.2%) exceeds 5% because each bar is ranked against its own past, not the full sample.

Performance: ~1.5 s median for 8,479 bars (7 runs), dominated by the Price Action Engine
(~1.4 s); the causal percentile takes ~0.1 s.

## Limitations

- Trend inherits Price Action's conventions (≈73% `transition` on SPY daily) and its 5-bar
  confirmation latency.
- Volatility is relative to the trailing 3 years; the first 272 bars have no label; a long calm
  period makes moderate volatility look "high".
- Momentum and participation thresholds are textbook conventions (RSI 70/30/50; 0.5×/1.5×).
- No breadth, macro, news or economic regimes.
- Regimes are not yet validated as informative about anything; that belongs to later phases.
