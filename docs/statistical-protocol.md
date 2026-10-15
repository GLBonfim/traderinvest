# Statistical Protocol — risk-profile research (pre-specified)

| | |
|---|---|
| Protocol | `TI-STAT-RISK` |
| Version | **1.0.0-draft** |
| Date | 2026-10-13 |
| Status | **DRAFT — proposed, not approved.** Nothing below is in force until the owner approves it (see *Pending decisions*). |
| Code base | commit `3c3738c` (`feature/research-validation-foundation`) |
| Related | [statistical-validation.md](statistical-validation.md) (Phase 9 method and formal family), [risk-management.md](risk-management.md) (overlays, research CLI, artifacts) |

This document fixes, **before any new run**, which questions the risk research may answer
confirmatorily, how, and under which conditions a test is invalid or not testable. It produces
no result. It does not change the engine, the strategies, the risk profiles, the costs or the
data. Methods it names that do not exist yet in the code (p-values for the risk overlays,
multiple-comparison corrections for them, a studentised Sharpe test, a forward slice) are
**not implemented** by this document; it only states what they must satisfy before use.

## 1. Principles

1. **Three tiers, never mixed.**
   - **E — exploratory (historical).** Every analysis of sessions already observed. It may
     show estimates, intervals and even p-values, but it never confirms anything.
   - **C — confirmatory (prospective, pre-specified).** Only hypotheses listed in §4, tested
     once, on sessions that **had not been observed** when this protocol was approved, with
     the methods and corrections fixed here.
   - **T — temporal robustness.** Consistency of direction across fixed historical slices
     (`early`, `late`). It is a property of the observed history, not out-of-sample
     validation (the slices are not independent and were all observed).
2. **No retroactive confirmation.** All SPY sessions up to **2026-10-12** have been observed in
   research runs (full slice, 8,483 sessions, 1993-01-29 → 2026-10-12; Steps 2B and 3), as has
   the Phase 9 sample. Results on those sessions — including the Step 2B/3 overlay comparisons
   and the Phase 9 formal family — are tier E evidence for this protocol, whatever their
   p-values.
3. **Hypotheses are not chosen by results.** The confirmatory family below is derived from the
   purpose of each overlay and from what is statistically testable, not from which historical
   differences looked favourable. Where a hypothesis was shaped by observed history, that is
   stated.
4. **Not testable is a valid outcome.** A hypothesis that does not meet its validity conditions
   is reported as *not testable*, counts in the family size, and is never replaced by a
   different test after the data are seen.

## 2. Research questions

| Id | Question | Tier |
|---|---|---|
| **Q1** (primary) | For the paper-traded strategy `sma_trend` under cost scenario D, does an **event-driven** risk overlay (`drawdown_lock_20`, `atr_stop_3`) change **net risk-adjusted performance** relative to `control_no_overlay`? | C |
| Q2 (secondary) | Do those overlays change the **tail risk**, volatility or mean of net daily returns? | C (secondary family) |
| Q3 | How do the four profiles compare on all metrics, slices and costs in the observed history? | E |
| Q4 | Are the directions of the differences consistent between `early` and `late`? | T |
| Q5 | Does `fixed_fraction_50` deliver the risk/return trade-off its construction implies? | E (descriptive only; see §4.3) |

The strategy-versus-benchmark question (does `sma_trend` beat buy & hold?) belongs to the
Phase 9 formal family ([statistical-validation.md](statistical-validation.md)), which was
declared before its results were seen and is not repeated here. A prospective confirmation of
it is decision **D7** (§11).

## 3. Units, data and conventions

- **Statistical unit:** the net daily return of one session, `daily_return` in
  `daily_returns.csv` (schema `risk-daily-returns/2`; earlier runs `/1`): equity(t)/equity(t−1)
  − 1 with equity(−1) = the initial capital; costs of scenario D deducted at the fills; raw
  prices; no dividends; no cash yield. The return is **undefined** when equity(t−1) = 0
  (ADR-0034 D-A5): written `null` in `/2`, never 0 or an estimate; a close that reaches 0 is a
  defined −1. `/1` files cannot contain an undefined return.
- **Pairing:** every comparison is paired session by session — the overlay and the control
  series of the same run, same strategy, same slice, **identical** session timestamps.
- **Annualisation:** 252 sessions per year; risk-free rate 0 (as in Phase 8/9). Because the
  engine credits no interest on cash, exposure-reducing overlays are compared on a basis that
  understates the return of their cash; this is a known bias against such overlays (§9).
- **Horizon:** a test uses all sessions of its pre-declared window and nothing else; there is
  no interim look (§6.5).

## 4. Hypothesis families

### 4.1 Confirmatory family C (primary) — Q1

Window: the forward window **W = [T0, T_end]** (§6), strategy `sma_trend`, cost scenario
`D_default`, control `control_no_overlay`.

| Id | H0 | H1 | Metric |
|---|---|---|---|
| **C1** | SR(`drawdown_lock_20`) − SR(control) = 0 | ≠ 0 (two-sided) | annualised Sharpe ratio of net daily returns |
| **C2** | SR(`atr_stop_3`) − SR(control) = 0 | ≠ 0 (two-sided) | idem |

- Family size **m = 2**; **Holm** at **α = 0.05** (family-wise error rate, valid under
  arbitrary dependence — the two overlays share the control and the same sessions).
- Two-sided: an overlay can also worsen risk-adjusted performance, and a one-sided test would
  need a direction justified before the data, which we do not have.
- A hypothesis that fails its validity conditions (§7) is *not testable*: it still counts in
  m = 2 and is reported as such (conservative, as in Phase 9).

**Why these two.** They are the profiles whose effect is *not* determined by construction: a
drawdown lock and a stop act only when specific events occur, so their effect on risk-adjusted
performance is an empirical question. Their inclusion was also influenced by the Step 3
history (no robust benefit observed), which is one more reason they must be confirmed — or
not — on unseen data.

### 4.2 Secondary family S — Q2

Same window and pairing as C. For each of `drawdown_lock_20` and `atr_stop_3` vs control:

| Id | Metric (difference overlay − control) |
|---|---|
| S1, S2 | expected shortfall (CVaR) of daily net returns at 5% (mean of the worst 5% of sessions) |
| S3, S4 | annualised volatility |
| S5, S6 | annualised mean daily net return |

- m = 6; **Benjamini–Hochberg** at q = 0.05, reported as **context for C**, never as
  confirmation and never used to rescue a non-significant C test. Using FDR here is justified
  because the family is descriptive of several risk dimensions and errors in it do not drive
  a decision on their own.
- Tail metric choice: CVaR is a function of the multiset of returns (order-invariant), so the
  block bootstrap of paired returns is defensible for it — unlike maximum drawdown (§5.2).

### 4.3 Excluded from inferential testing (with reasons)

| Item | Status | Reason |
|---|---|---|
| `fixed_fraction_50` vs control: Sharpe | **not informative** | Scaling every exposure by a constant f scales the mean and the volatility of returns by about f, so the Sharpe ratio is unchanged by construction up to costs and rebalancing-band drift; H0 is approximately true *by design*, and a test would spend α on a known answer. |
| `fixed_fraction_50` vs control: volatility, mean | **not informative** | Lower volatility and lower mean follow from the construction. |
| `fixed_fraction_50` (Q5) | descriptive only | Its value is a **policy choice** (risk tolerance), not a statistical question. |
| Maximum drawdown and drawdown duration (all profiles) | **descriptive only, no p-values or intervals claimed as inference** | Order-dependent metrics: block resampling of realized returns breaks drawdown paths and does not re-run the policy; the existing intervals are exploratory and were shown to be biased (Step 4; [statistical-validation.md](statistical-validation.md#kinds-of-results-how-to-read-them)). |
| CAGR / cumulative return differences | descriptive | Compounding makes intervals very wide and skewed; the information is carried by the mean return (S5, S6). |
| Number of trades, costs, exposure | descriptive | Properties of the rules, reported to interpret C and S, not hypotheses. |
| `volatility_target_10`, `atr_risk_1pct` | out of scope v1 | Not among the four profiles under evaluation for paper trading; may enter a later version only through an amendment made before the relevant data are observed (§10). |

### 4.4 Exploratory family E — Q3

All four profiles × all slices (`full`, `early`, `late`) × costs actually run × all metrics of
the risk table, on observed history. Allowed: point estimates, exploratory bootstrap
intervals, and — only once a validated p-value method exists — p-values with Holm adjustment
shown **for orientation**, labelled "exploratory, data already observed". Not allowed: any
statement of confirmation, or selection of confirmatory hypotheses from these results.

### 4.5 Temporal robustness T — Q4

For each overlay and each metric of C and S: the sign of the difference vs control in `early`
and in `late`, reported as consistent / inconsistent / undefined. No p-values. It describes the
observed history only.

## 5. Metrics — assessment

| Metric | Role | Strengths | Limitations |
|---|---|---|---|
| Sharpe ratio (annualised, rf = 0) | **primary (C)** | Risk-adjusted; smooth function of the mean and variance of returns, so paired block-bootstrap inference is standard; invariant to leverage, which isolates the *quality* of returns from their *size* | Ignores tail shape and path; sensitive to non-normality and serial dependence; rf = 0 and no cash yield; estimation error is large over short windows (§6.3) |
| Expected shortfall 5% | secondary (S) | Direct tail-risk measure; order-invariant, bootstrap-defensible | Needs many sessions for a stable 5% tail; estimates the daily tail, not drawdown risk |
| Annualised volatility | secondary (S) | Precisely estimated | Symmetric; says nothing about return |
| Annualised mean return | secondary (S) | What compounds | Noisy; its difference is partly mechanical for exposure-reducing overlays |
| CAGR | descriptive | Economically intuitive | Path-dependent compounding; wide skewed intervals |
| Maximum drawdown, drawdown duration | descriptive | What an operator feels | Order-dependent; a single path; no valid interval method available (§4.3) |
| Costs, orders, trades, exposure, interventions | descriptive | Explain the mechanism | Not hypotheses |

**Why Sharpe as primary, and why it is not enough.** Q1 asks whether an overlay improves the
return per unit of risk; the Sharpe ratio is the standard, inferentially tractable measure of
exactly that. It does not by itself show economic value: a higher Sharpe can come with a lower
mean return, and drawdown protection — often the reason for a lock or a stop — is not captured.
Hence the secondary family and the descriptive drawdown reporting. Alternatives considered:

| Option | Consequence |
|---|---|
| **A. Sharpe difference (recommended)** | Tractable, leverage-invariant; misses tails and path. |
| B. Expected-shortfall difference as primary | Captures tail risk; needs larger samples; does not reward return. |
| C. Mean-return difference as primary | Measures the economic cost/benefit; for exposure-reducing overlays largely mechanical; noisy. |
| D. Maximum-drawdown difference as primary | Closest to the overlays' intent but **not testable** with any validated method available (rejected). |

## 6. Inference methods and assumptions

### 6.1 p-values

- **Required property:** correct size (rejection rate ≈ α under H0) for paired, serially
  dependent, heteroscedastic, heavy-tailed daily returns, at the window length of the test.
- **Candidate methods:**
  1. *Paired moving-block bootstrap, centred* — the existing Phase 9 primitive
     (`centered_p_value`, non-circular blocks, same index matrix for both series). Simple and
     consistent with Phase 9, but not studentised: size can be poor in short windows and for
     ratio statistics.
  2. *Studentised time-series bootstrap for the Sharpe difference* (Ledoit & Wolf, 2008),
     with a HAC standard error and block resampling. Designed for this problem; better size in
     small samples. Not implemented.
- **Recommendation:** method 2 for C1/C2 and method 1 (CVaR, volatility, mean) for S, **only
  after** each implementation has passed a simulation check of its size on synthetic paired
  series with realistic autocorrelation, heteroscedasticity and fat tails, at the planned
  window length (decision **D3**). Until then, C and S cannot be evaluated.

### 6.2 Dependence

Daily returns show volatility clustering (heteroscedasticity) and weak autocorrelation; the
overlay and control series are strongly cross-correlated. Paired resampling with a common index
preserves the cross-dependence; blocks preserve serial dependence up to their length. The
block length (L = 21 in Phase 9) must be fixed before T0 (D4); with a short window the number
of blocks n/L becomes small and bootstrap distributions coarse — one reason for a minimum
window (§6.3).

### 6.3 Sample size and minimum events

- **Window length.** The standard error of an annualised Sharpe ratio estimated from N years is
  roughly √((1 + SR²/2)/N) for one series (Lo, 2002); paired differences of highly correlated
  series are estimated more precisely, but short windows still detect only large effects.
  Before T0, the minimum detectable Sharpe difference for each of C1/C2 must be computed from
  the **historical (tier E) variance of the paired difference** — planning use of observed
  data, not confirmation — and the window length chosen so that it is meaningful (D2).
- **Minimum events.** An event-driven overlay only differs from the control when it acts. Each
  C/S hypothesis requires at least **5 intervention events** of the overlay in W (stop
  activations — one per position episode, ADR-0034 D-A1 — for `atr_stop_3`; transitions of the
  drawdown monitor into LOCKED for `drawdown_lock_20`) — the documented
  `SPARSE_EVENT_THRESHOLD` of the research artifacts — or it is *not testable*. Historical
  frequency (tier E) suggests such events are rare (e.g. no stop trigger in the full-sample
  `sma_trend` run), so **C1/C2 may well be not testable for years**; that outcome is accepted
  in advance rather than replaced by a weaker test.
- **Minimum trades.** At least **10 completed round trips** of the control in W (decision D2
  may raise it); fewer make any comparison of a timing strategy's overlays uninformative.

### 6.4 Multiplicity

| Family | Size | Correction | Error controlled |
|---|---|---|---|
| C | 2 | Holm, α = 0.05 | FWER, any dependence |
| S | 6 | Benjamini–Hochberg, q = 0.05 | FDR (assumes independence or positive regression dependence — plausible for these positively related metrics, but not guaranteed; hence secondary only) |
| E | — | Holm shown for orientation only | none claimed |
| T | — | none (no p-values) | — |

Not-testable hypotheses count in their family's m.

### 6.5 One look

C and S are evaluated **once**, at T_end. No interim analysis, no extension of W after
looking, no change of method. If interim monitoring is ever wanted, it must be added by an
amendment **before T0** with an alpha-spending rule.

## 7. Validity conditions and exclusions

A C or S test is **valid** only if all hold; otherwise it is reported *not testable* (or
*invalid*, with the reason):

1. The run was produced by the commit recorded for this protocol version, with
   `git_dirty: false`, and `app.risk.cli verify` reports `ok` (schema `risk-research-run/3`).
2. `db_access.read_only_confirmed` is true and the data fingerprint is recorded.
3. Strategy, profiles, cost scenario, initial capital and slice in the manifest equal those
   declared here.
4. The overlay and control series cover exactly the same sessions, all inside W, with no
   missing session; the session count meets the minimum (D2).
5. No non-finite value and no undefined (`null`) return in either series in W. **No
   imputation, no dropping of sessions, no approximate alignment.** A series with an undefined
   return anywhere in the run is refused by `load_returns_matrix`, so the test is *not
   testable*; an undefined return is never replaced by 0 to make a test testable.
6. Minimum events and trades (§6.3) are met.
7. The p-value method has passed its size check (§6.1) before T0.
8. The protocol version in force was approved before T0, and no amendment touching the test
   was made after any session of W was observed.

An *invalid* or *not testable* outcome is final for that protocol version.

## 8. Consuming `daily_returns.csv`

1. Verify the run (`verify_run` / `app.risk.cli verify`); stop if it is not `ok`.
2. Load with `load_returns_matrix(run_dir, slice_name=…, strategy="sma_trend",
   scenarios=["control_no_overlay", <overlay>])`, which parses the very bytes it verified and
   refuses missing, duplicated or misaligned series and any series containing an undefined
   (`null`) return — that refusal makes the hypothesis *not testable* (§7, condition 5). The
   exploratory `risk_comparisons.csv` marks such comparisons `not testable` in its `note`, and
   `risk_table.csv` gives the cause of a missing return-based metric in its `*_status`
   columns; neither is a confirmatory result.
3. Restrict to W by timestamp; check the session count and that both columns cover the same
   sessions (the matrix guarantees equal indices).
4. Check from the manifest: cost scenario `D_default`, `initial_capital`, strategy, profile
   fingerprints, backtest fingerprint, `numpy_version`, commit, data fingerprint.
5. Compute the paired differences only after (1)–(4); keep strategy returns, control returns
   and paired differences as separate, labelled objects.
6. Record provenance in the result: run id, manifest SHA-256, protocol version, commit, data
   fingerprint, W, session count, event counts.

## 9. Known limitations (apply to every tier)

- One instrument (SPY), one historical path, daily bars; overlays are single-instrument and
  long-only.
- Raw prices, no dividends credited to strategies; no interest on cash (rf = 0); scenario D
  costs are assumptions.
- The strategy and the overlays were designed with the full history available; even a forward
  window tests them only from T0 on.
- A forward window requires running the overlays on bars after T0 with the research CLI; the
  CLI currently offers only the fixed slices `full`, `early`, `late` (§11, D1).
- Integrity checks show self-consistency of a run, not authenticity
  ([risk-management.md](risk-management.md)).

## 10. Amendments

- The protocol is versioned (`MAJOR.MINOR.PATCH`); this file keeps every version: a new
  version is added **above** the previous one and earlier sections are never edited or deleted
  (git history is the second record). A `Changelog` table lists version, date, author, reason
  and the hypotheses affected.
- An amendment affecting a C or S hypothesis applies only to windows starting after the
  amendment is approved; made after any session of the affected window was observed, it
  demotes that hypothesis to tier E.
- Clarifications that change no hypothesis, metric, method, threshold or window are PATCH
  versions.

## 11. Pending decisions (owner)

| Id | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | How the forward window W is simulated | (a) add a pre-declared `forward` slice (start = T0) to the research CLI; (b) generic `--start/--end` options | (a): a single, fixed, pre-declared slice keeps W from being chosen after the data are seen |
| D2 | T0, T_end and minimum sessions | T0 = first session after approval; T_end fixed as a date or a session count | Fix T_end by session count after the minimum-detectable-effect calculation (§6.3); require at least 504 sessions (~2 years) unless that calculation justifies otherwise |
| D3 | p-value method for C | centred bootstrap (existing) vs studentised Ledoit–Wolf | Ledoit–Wolf for C1/C2, after a simulation size check |
| D4 | Block length for W | keep L = 21; data-driven rule fixed now | Keep L = 21 for comparability with Phase 9; revisit only by amendment before T0 |
| D5 | Secondary metric set | as in §4.2; add downside deviation; drop mean | As in §4.2 |
| D6 | Include `fixed_fraction_50` anywhere inferentially | no (§4.3); yes, as a sanity check of the method | No inferential test; use it only to sanity-check that the Sharpe test does **not** reject a construction-implied null (validation of the method, not a hypothesis) |
| D7 | Prospective strategy-vs-benchmark confirmation | none; add `sma_trend` vs buy & hold (mean, Sharpe) as family C2 | Not in v1: low power over a short window and outside the risk-profile question |
| D8 | Approval and freezing | approve this draft as 1.0.0; amend first | Approve after D1–D4 are decided, then record the protocol file's SHA-256 with the approval commit |

## Changelog

| Version | Date | Change | Hypotheses affected |
|---|---|---|---|
| 1.0.0-draft | 2026-10-13 | First proposal; no result produced | C1–C2, S1–S6 defined; none evaluated |

## References

- Künsch, H. R. (1989). The jackknife and the bootstrap for general stationary observations.
- Lo, A. W. (2002). The statistics of Sharpe ratios.
- Ledoit, O. & Wolf, M. (2008). Robust performance hypothesis testing with the Sharpe ratio.
- Holm, S. (1979). A simple sequentially rejective multiple test procedure.
- Benjamini, Y. & Hochberg, Y. (1995). Controlling the false discovery rate.
