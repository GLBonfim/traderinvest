# Machine Learning (Phase 10)

> **Result (pre-declared rule): NO INCREMENTAL EDGE FOUND** — for both models.
> Phase 10 asks whether supervised ML adds information beyond the Phase 7 baselines. It is a
> research experiment, not an optimisation and not a deployment. No model is used for trading.

Code: `app/ml/` (`config.py`, `features.py`, `target.py`, `split.py`, `preprocessing.py`,
`models.py`, `training.py`, `evaluation.py`, `engine.py`, `cli.py`) · ML version `1.0.0` ·
feature version `1.0.0`.

## Protocol (everything fixed before any model result was seen)

| Item | Value |
|---|---|
| Target | `next_session_close_to_close_up` = 1 if raw close(T+1) > close(T), else 0 |
| Split | TRAIN ≤ 2009-12-31 · VALIDATION 2010-01-01 – 2017-12-31 · TEST ≥ 2018-01-01 |
| Purge | last session of TRAIN and of VALIDATION dropped (target horizon 1) |
| Models | logistic regression (L2, C = 1, lbfgs, no class weights) · random forest (300 trees, depth 4, ≥ 50 per leaf, sqrt features, n_jobs 1) |
| Hyperparameter search | none |
| Decision rule | P(up) > 0.5 → LONG, else FLAT; undefined → INSUFFICIENT_DATA |
| Seed | 20261007 (models and bootstrap) |
| Validation use | reported only; no configuration was selected on it |
| Test | evaluated once, at the end |
| Edge rule | see below |

Split rationale: TRAIN ends on the boundary already used by the Phase 9 `early` slice; the
remaining 16.75 years are halved at 2018 into 8 years of validation and ~8.75 years of test
(incl. 2020 and 2022). Rows: TRAIN 3,991 · VALIDATION 2,012 · TEST 2,201 · purged 2 · warm-up
(insufficient features) 272 · last row without target 1 (= 8,479).

Class balance (share of up sessions): TRAIN 52.7% · VALIDATION 54.7% · TEST 54.9%. Mild
imbalance; no class weighting (declared in advance).

### Pre-declared incremental-edge rule

A model shows INCREMENTAL EDGE on TEST only if **all** hold: (1) ROC-AUC 95% block-bootstrap
lower bound > 0.5; (2) Sharpe difference vs the price benchmark **and** each of the 5 timing
baselines is positive with Holm-adjusted p < 0.05 (family = 2 models × 6 references = 12);
(3) at 10 bps costs, its net Sharpe exceeds the price benchmark's. Otherwise: NO INCREMENTAL
EDGE FOUND. Beating one weak baseline is explicitly insufficient.

## Timing model

```
features(T)  — known at the close of T (observed_at), from bars <= T only
prediction   — made after the close of T
action       — Phase 7/8 convention: effective at the open of the next XNYS session
target(T)    — uses close(T+1); known at the close of T+1 (target_timestamp); never a feature
```

The last row has no T+1: its target is NaN (excluded from fitting/scoring) but a prediction is
made, and the resulting decision stays pending in the backtest (Phase 8 final-bar rule).

**Known mismatch (documented, not changed):** the close-to-close target includes the overnight
gap close(T) → open(T+1), which next-open execution cannot capture.

## Features (86; catalog with group, kind, optional flag and definition in code)

| Group | Count | Content |
|---|---|---|
| candle | 7 | body / wick ratios, close position, gap, range/close, body/close |
| pattern | 20 | binary flag per Phase 3 pattern, stamped at the pattern's last candle |
| indicator | 19 | close/SMA−1 and close/EMA−1 (20/50/200), MACD/close (line, signal, histogram), RSI14, %K, %D, ROC12, ATR/close, Bollinger width and %B, realized vol, relative volume, OBV flow |
| price_action | 20 | structure one-hot (confirmed pivots), trend quality, support/resistance distance, support zone width, consolidation, window width, range ratios, 8 event flags with `available_at == T` |
| regime | 20 | volatility percentile; one-hot volatility, momentum, participation (incl. insufficient), composite |

**ML-specific representations (source engines unchanged):** SMA/EMA, MACD and OBV are price-
level quantities; SPY rises ~18× over the sample, so a model fitted on 1994–2009 would see
out-of-range values later. They are expressed scale-free (distances, /close,
`(OBV − OBV[t−20]) / Σ volume[20]`). Candle-pattern `strength` is not used (geometric
description only; flags suffice for a first version).

**Excluded on purpose:** the Price Action `outcomes` table (known only later), adjusted close,
any T+1 value.

**Validity:** a row is `valid` when all 72 required features are finite and structure,
volatility and momentum regimes are known (first valid row 1994-02-25, after the 272-bar
regime warm-up). 14 optional features may be undefined on valid rows (e.g. `trend_quality`
undefined on 73% of rows, `resistance_distance_pct` on 16.8% — at all-time highs there is no
resistance zone).

## Preprocessing (fitted on TRAIN only, frozen)

Optional features get a missing-indicator column and the TRAIN median as fill value; all 100
resulting columns are standardised with TRAIN mean/std (ddof 0; std 0 → 1). A NaN in a
required feature raises (such rows are never scored). Tested: wrecking every non-TRAIN row
leaves the fitted parameters unchanged; a full-sample fit would differ (control).

## Results (TEST is primary; TRAIN is diagnostic only)

### Classification

| Model | Period | Accuracy | Always-up accuracy | Balanced acc. | Precision | Recall | ROC-AUC [95% block CI] | Predicted up |
|---|---|---|---|---|---|---|---|---|
| logistic | train (diag.) | 0.569 | 0.527 | 0.562 | 0.576 | 0.687 | 0.593 | 62.8% |
| logistic | validation | 0.521 | 0.547 | 0.510 | 0.555 | 0.630 | 0.515 [0.495, 0.539] | 62.1% |
| logistic | **test** | **0.525** | **0.549** | 0.516 | 0.562 | 0.609 | **0.519 [0.495, 0.542]** | 59.5% |
| forest | train (diag.) | 0.590 | 0.527 | 0.574 | 0.573 | 0.865 | 0.649 | 79.5% |
| forest | validation | 0.520 | 0.547 | 0.491 | 0.542 | 0.797 | 0.502 [0.484, 0.524] | 80.5% |
| forest | **test** | **0.530** | **0.549** | 0.505 | 0.552 | 0.759 | **0.505 [0.483, 0.528]** | 75.5% |

Confusion matrices (test, tn/fp/fn/tp): logistic 419/574/472/736 · forest 249/744/291/917.
Both models are **less accurate than always predicting "up"** out of sample, and both AUC
intervals include 0.5. The drop from TRAIN to VALIDATION/TEST indicates overfitting (larger for
the forest).

### Financial (TEST 2018-01-02 → 2026-10-06, Phase 8 default costs, net)

| Subject | Cum. return | CAGR | Vol | Sharpe (gross) | Sortino | Max DD | Trades | Exposure | Ann. turnover | Costs |
|---|---|---|---|---|---|---|---|---|---|---|
| ML logistic | 1.07 | 8.7% | 16.2% | 0.59 (0.76) | 0.84 | −31.3% | 374 | 59.5% | 86 | 31,839 |
| ML forest | 1.44 | 10.7% | 18.0% | 0.66 (0.74) | 0.93 | −34.2% | 219 | 75.5% | 49 | 20,747 |
| buy_and_hold / price benchmark | 1.91 | 13.0% | 19.0% | 0.74 | 1.03 | −34.1% | 0 | 100% | 0.07 | 31 |
| total-return benchmark | 2.32 | 14.7% | 19.0% | 0.82 | 1.15 | −33.7% | 0 | 100% | 0.06 | 31 |
| sma_trend | 0.97 | 8.1% | 12.5% | 0.68 | 0.92 | −22.5% | 29 | 80.1% | 5.9 | 2,060 |
| sma_crossover | 0.86 | 7.4% | 12.1% | 0.65 | 0.88 | −29.8% | 18 | 71.0% | 4.1 | 1,506 |
| rsi_momentum | 0.75 | 6.6% | 11.2% | 0.63 | 0.86 | −21.3% | 111 | 69.8% | 25.7 | 9,231 |
| price_action_trend | −0.08 | −0.9% | 8.1% | −0.08 | −0.09 | −24.4% | 19 | 24.0% | 4.3 | 1,068 |
| regime_trend | −0.08 | −0.9% | 8.1% | −0.08 | −0.09 | −24.4% | 19 | 24.0% | 4.3 | 1,068 |

(`price_action_trend` and `regime_trend` are identical on this window — consistent with the
Phase 9 `late` redundancy finding.)

### Formal family (TEST, 12 tests, Sharpe difference ML − reference)

| Model | vs price bench | vs sma_trend | vs sma_crossover | vs rsi_momentum | vs price_action | vs regime |
|---|---|---|---|---|---|---|
| logistic | −0.14 [−0.50, 0.21] p_Holm 1.00 | −0.09 p_Holm 1.00 | −0.05 p_Holm 1.00 | −0.03 p_Holm 1.00 | +0.67 [0.11, 1.27] p 0.026 → **p_Holm 0.25** | +0.67 → **p_Holm 0.25** |
| forest | −0.08 [−0.29, 0.15] p_Holm 1.00 | −0.03 p_Holm 1.00 | +0.01 p_Holm 1.00 | +0.03 p_Holm 1.00 | +0.73 [0.20, 1.34] p 0.014 → **p_Holm 0.17** | +0.73 → **p_Holm 0.17** |

No test survives Holm (or BH: minimum 0.077). The only raw p < 0.05 are against the two
redundant, weakest baselines — exactly the "beats one weak baseline" situation the protocol
rules out.

### Cost sensitivity (TEST, Sharpe / CAGR)

| Subject | A 0 bps | B 5 bps | C 10 bps | D default |
|---|---|---|---|---|
| ML logistic | 0.76 / 11.6% | 0.49 / 6.9% | 0.23 / 2.4% | 0.59 / 8.7% |
| ML forest | 0.74 / 12.5% | 0.60 / 9.7% | 0.46 / 7.0% | 0.66 / 10.7% |
| price benchmark | 0.74 / 13.0% | 0.74 / 13.0% | 0.74 / 13.0% | 0.74 / 13.0% |

High turnover (49–86× per year) makes both models very cost-sensitive; at 0 bps they are at
best comparable to buy & hold.

### Robustness windows (default costs; Sharpe)

| Window | Role | ML logistic | ML forest | Price benchmark |
|---|---|---|---|---|
| early (≤ 2009) | **in-sample, diagnostic only** | 1.05 | 0.83 | 0.38 |
| validation (2010–2017) | out-of-sample | 0.64 | 0.62 | 0.81 |
| test (2018–) | out-of-sample, primary | 0.59 | 0.66 | 0.74 |
| late (2010–) | out-of-sample | 0.61 | 0.64 | 0.76 |
| full | mixed, diagnostic only | 0.84 | 0.74 | 0.55 |

The striking in-sample numbers (early, and therefore full) disappear out of sample: a textbook
overfitting signature. In-sample and mixed windows must not be read as evidence.

### Verdict

| Model | AUC lower bound > 0.5 | Sharpe beats all 6 references after Holm | Beats benchmark at 10 bps | Verdict |
|---|---|---|---|---|
| logistic regression | no (0.495) | no | no (0.23 vs 0.74) | **NO INCREMENTAL EDGE FOUND** |
| random forest | no (0.483) | no | no (0.46 vs 0.74) | **NO INCREMENTAL EDGE FOUND** |

## Reproducibility and outputs

Same data + configuration + seed → bit-identical probabilities (tested, tolerance 0; forest
single-threaded). Each run writes `data/ml/<experiment_id>/` (git-ignored): `experiment.json`
(experiment id, git commit — suffixed `+dirty` if the tree had uncommitted changes —, versions,
fingerprints, full config incl. split dates, target definition, sample counts, timings,
verdict) and CSVs (`class_distribution`, `classification`, `auc_intervals`, `financial`,
`comparisons`, `verdicts`, `model_parameters`). CLI: `uv run python -m app.ml.cli run --symbol SPY`.

Runtime (full SPY run ≈ 49 s): features + target 3.2 s · preprocessing + training 1.1 s ·
prediction 0.09 s · baseline strategies 3.1 s · backtests (5 windows × 4 scenarios) 28.4 s ·
bootstrap statistics 1.0 s.

## Limitations

- One instrument, one path; ~8.75 years of test data; 2 model families counted as multiple
  experimentation (12-test family).
- The close-to-close target is not aligned with next-open execution (overnight gap).
- No retraining (models fitted once on 1994–2009 and frozen through 2026); regime changes over
  16 years out of sample are not adapted to — deliberately, to keep the test clean.
- Linear and shallow-tree models only; feature set is the existing handcrafted one.
- A negative result here does not prove no predictive information exists anywhere; it shows
  none was found under this pre-declared protocol.
