# Multi-Asset Trend Strategy — Design v2

Date: 2026-11-14
Status: approved by the owner on 2026-11-14 with the corrections recorded in section 17; T1 decisions and the approved asset list in section 18.
The owner pre-approved implementation of the OFFLINE scope below, phase by phase; each phase
(T0–T4) ends with a report and waits for explicit approval.
Branch: `feature/multi-asset-trend`, created from `main`.
Supersedes in part: [2026-11-04 design v1](2026-11-04-simple-multi-asset-trend-bot-design.md).
Decision record: ADR-0040 in `docs/decisions.md`.

## 1. Why a version 2

Every intraday FX agent (M15, H1, M5) and every SPY timing rule tested in this repository
failed its pre-registered validation. The owner decided to stop that line of work and test one
slow, multi-asset trend strategy. Version 1 described a gold-first, three-sleeve, monthly
10-month-average design. Version 2 replaces the *strategy definition, universe, evidence
thresholds and delivery plan* with fixed values chosen by the owner, while keeping the
principles of v1.

The goal is unchanged: an honest answer to one question, not a profitable-looking backtest.

> Can one fixed, slow, long-or-cash trend rule, applied identically to a diversified universe
> and sized by volatility, beat EUR cash and a passive equal-weight portfolio after realistic
> EUR costs, on a sealed period that was never used to build it?

If the answer is no, the work stops. No variant may be created to rescue the result.

## 2. What changes from v1

| Topic | v1 (2026-11-04) | v2 (this document) |
|---|---|---|
| Universe | Gold first, then 3 sleeves (gold, global equities, euro govt bonds) | 10–15 diversified exposures from the start, fixed in T1 before any result |
| Decision cadence | Last session of each month | Every daily close |
| Signal | Month-end level vs 10-month SMA | Daily EUR total-return close vs 200-session SMA |
| Sizing | Fixed 1/3 per active sleeve | `w_i = (1/N) × min(1, 10% / vol_i)`, `vol_i` = 60-session realised vol |
| Execution | Next valid open | Close of the next session (`T+1`) for every asset; open prices are never used |
| Rebalancing | Pre-registered operational tolerance | Trade only on state change or > 25 % relative drift from target |
| Idle cash | Broker interest tiers per capital | €STR − 0.25 p.p. (negative retained) |
| Capital grid | 200 … 50 000 EUR | 500, 2 000, 5 000, 10 000, 25 000 EUR |
| Sharpe gate | ≥ 0.30 | ≥ 0.40 **and** above equal-weight buy-and-hold |
| Drawdown gate | ≤ 20 % | ≤ 20 % **and** below equal-weight buy-and-hold |
| Concentration gate | No sleeve > 70 % of positive P&L | No asset > 40 % of positive P&L |
| Stress costs | 2× commission, spread, slippage | 2× every cost (commission incl. minimum, spread, slippage, TER without double counting) |
| Verdict | Gates only | Gates + one-sided 95 % bootstrap bound of the excess Sharpe; consequences fixed in advance |
| Benchmarks | Continuously invested equal weight | Equal-weight always invested, global 60/40, EUR cash |
| Gold-only stage | Stage 1 | Removed |
| Delivery end | Offline gold engine + causal tests | Sealed test run once, final verdict |

## 3. What remains in force from v1

Unless this document says otherwise, v1 still applies, in particular:

- intent, non-goals and anti-goals (no leverage, no shorts, no derivatives, no live learning,
  no LLM decisions, no holdout reuse, no hidden costs);
- the owner context: resident in Portugal, accounting in EUR, intended broker IBKR Ireland;
- "what the bot knows": structured, point-in-time inputs only; no news or narratives;
- causal replay mechanics, fail-closed data handling, immutable run records (v1's "next
  valid open" fill is replaced by the next session's close, section 6.4);
- the distinction between research proxies and executable products;
- the risk and safety section and the later operational stages (IBKR paper, EUR 200
  micro-live, scaled live, prop-firm assessment). None of those stages is authorised by this
  delivery; each requires a separate design and approval;
- ADR-0006: real-money execution stays blocked.

## 4. Scope and constraints of this delivery

- **Offline only.** No broker connection, no orders, no paper account, no real money.
- **Frozen packages.** `app/fx`, `app/desktop`, `app/engine`, `app/paper`, `app/broker`,
  `app/ml`, `app/candles`, `app/price_action` are not modified or deleted. No new M5/M1
  training rounds.
- **Isolated code.** New code lives in `app/trend/` with tests in `tests/unit/trend/`. Reuse is
  limited to narrow interfaces:
  - `app.validation.resampling` — moving-block bootstrap (`moving_block_indices`,
    `bootstrap_summary`);
  - `app.core.integrity` — `canonical_json`, `atomic_write`, hashing;
  - the *pattern* of `app/fx/research/holdout_guard.py` (exclusive creation, permanent claim,
    no reset API), re-implemented in `app/trend` because `app/fx` is frozen and must not
    become a dependency.
  Diffs outside the new package are limited to docs, `CHANGELOG.md` and, if needed, a CLI
  entry point.
  `main` predates `app/core/integrity.py` and `bootstrap_summary`; both files were copied
  byte-identical from `feature/fx-desktop-platform` so that a later merge cannot conflict.
- **Free data only.** If free data cannot meet section 7, work stops and the gap is reported.
- **Fixed parameters.** 200, 60 and 10 % are the only free parameters. No alternatives are
  run, printed or inspected, including "just for information".
- **Hard end.** This delivery ends with the single sealed test. A `NO STATISTICAL EDGE FOUND`
  or `INSUFFICIENT EVIDENCE` verdict ends the work.
- Conventions: English code and technical docs; `uv`, `ruff`, `mypy`, `pytest`; owner reports
  in plain Portuguese.

## 5. Universe

### 5.1 Selection rules (fixed now, list fixed in T1)

The universe has 10 to 15 exposures. T1 proposes the list, with a written justification per
exposure, **before** any strategy, moving-average or return statistic is computed. The list is
then frozen in the manifest. Selection uses only economic and practical criteria:

1. **Diversification by economic driver.** Coverage of: equities by region (e.g. US, Europe,
   Japan/Pacific, emerging markets), government bonds (euro area; possibly global/US),
   corporate bonds (investment grade), gold, broad commodities, listed real estate.
   No two exposures that are near-duplicates (expected long-run correlation above ~0.9).
2. **Liquidity.** Each exposure maps to a large, liquid UCITS ETF (or a physically backed ETC
   for gold, where UCITS is not possible) listed with a EUR trading line on a venue reachable
   from IBKR Ireland (e.g. Xetra, Euronext). Preference for accumulating share classes, which
   match total-return semantics.
3. **History.** A durable total-return history (product or documented index/proxy) is
   available for free.
4. **No performance-based choice.** No exposure is included, excluded, or swapped because of
   its past return, its past trend behaviour, or its effect on this strategy. Both the owner
   and the builder know broad market history; this limitation is declared, and the list is
   justified only on the criteria above.

`N` in the sizing formula is the fixed size of the frozen universe, not the number of active
assets.

### 5.2 Research series vs executable product

Each exposure has two records in the manifest:

- the **research series**: the free daily total-return history actually used (product NAV or
  price, index, or a longer-lived proxy such as a US-listed ETF), with its provider, currency,
  venue calendar, timezone, start date, dividend treatment and any splice point;
- the **executable product**: the UCITS ETF/ETC that would be traded (ISIN, ticker, venue,
  trading currency, TER, inception date, accumulating/distributing, replication method).

Where the product is younger than the required history, the research series is a declared
proxy. Proxies are never described as tradable. Splices (proxy → product) happen at a date
fixed in the manifest, never chosen after results.

## 6. Strategy (pre-registered, no tuning)

### 6.1 Time and data conventions

- **Asset series.** Daily close of a total-return level, converted to EUR. Each asset uses its
  own venue calendar.
- **EUR conversion.** ECB euro foreign exchange reference rates (published around 16:00 CET).
  The EUR level of a non-EUR asset on day `T` uses the reference rate of `T`; if none exists
  (TARGET holiday), it uses the latest earlier rate, at most 4 TARGET business days old;
  otherwise fail closed.
- **Decision time `D(T)`.** One daily decision at 23:00 Europe/Lisbon on master-calendar day
  `T`, after every relevant close and after the ECB rate of `T` is published. At `D(T)` each
  asset's input is its latest close at or before `D(T)`. The US close (16:00 New York, 20:00
  or 21:00 Lisbon) is later than the Xetra close (17:30 CET) and the ECB rate (~16:00 CET);
  all three precede `D(T)` on every day, including the weeks when US and EU daylight-saving
  dates differ. Signal and EUR conversion use only values published before `D(T)` (owner
  condition (b) at T1); a T2 test checks it for every input.
- **Master calendar.** All weekdays on which at least one universe asset has a session. An
  asset without a session on a master day carries its last close (zero return that day).
- **Sessions.** "200 sessions" and "60 sessions" count the asset's *own* sessions.

### 6.2 Signal (per asset, on closed bars only)

Let `P_t` be the asset's EUR total-return close and `SMA200_t` the simple mean of the last 200
closes including `P_t`.

- `INSUFFICIENT_DATA` while fewer than 200 closes exist. This state never opens or closes a
  position.
- `LONG` when `P_t > SMA200_t` (strictly above).
- `CASH` when `P_t <= SMA200_t`.

### 6.3 Volatility and target weight

- `r_t = P_t / P_{t-1} − 1` (simple daily EUR total return, asset's own sessions).
- `vol_t = stdev(r over the last 60 sessions, ddof = 1) × sqrt(252)`.
- Target weight, as a fraction of portfolio NAV at `D(T)`:
  - `LONG`: `w = (1/N) × min(1, 0.10 / vol)`;
  - `CASH` or `INSUFFICIENT_DATA` with no position: `w = 0`.
- `vol` undefined, zero, or computed over a stale window (more than 5 consecutive zero
  returns on asset sessions) is a data error: the asset fails closed (no trade) and the run is
  flagged.
- No redistribution: weight not used by an asset stays in cash. `Σ w ≤ 1` by construction;
  no leverage, no shorts.

### 6.4 Trading rule

At `D(T)`, an asset generates an order only if:

1. its state changed (`LONG ↔ CASH`), or
2. it is `LONG` and `|w_actual − w_target| / w_target > 0.25`, where `w_actual` is the market
   value weight at `D(T)`.

A trade always goes to the target (full exit on `CASH`). Then:

- **Fill at the next close (one rule for every asset).** The order fills at the asset's
  first close strictly after `D(T)` (its next session, `T+1`, on its own calendar; weekends
  and holidays skipped). Open prices are never used. A decision without a future close stays
  pending and is never filled synthetically. Between `D(T)` and the fill the asset keeps its
  previous position; decisions at `D(T+1)` see the pending order and do not duplicate it.
- **Sizing in units.** Quantity is computed from `D(T)` prices: whole units only, rounded
  toward zero. An order with zero quantity change is not sent.
- **Unit price.** For the proxy era, the research series is rescaled so that its EUR level on
  a fixed anchor date (recorded in the manifest) equals the executable product's EUR price
  that day. This gives realistic unit sizes for the capital grid.
- **Sequencing and cash.** Within one master-calendar day, sells fill before buys. Buys are
  reduced (whole units) so cash never becomes negative after costs; buy quantities are
  computed from `D(T)` prices and capped at fill time by the cash actually available.

### 6.5 Cash

Uninvested EUR earns the daily €STR minus 0.25 percentage points per year (act/360,
negative rates retained). Before €STR was published, the proxy is EONIA − 8.5 bp (the
official ECB relationship from October 2019). The same series is the "EUR cash" benchmark and
the risk-free rate for Sharpe ratios.

Declared limitation: IBKR may pay no interest on small balances. The report states IBKR's
actual interest rule on the date of the cost survey next to the results.

## 7. Data requirements and audit (T1)

Before any strategy result:

- **Sources.** Free, reproducible downloads only. Each raw file is stored unmodified with
  SHA-256, retrieval timestamp, URL/provider and licence notes.
- **History.** At least 15 years of evaluable common history **after** the 200-session
  warm-up of every asset (so the sealed test has at least 3 years).
- **Total-return semantics.** Documented for every series: gross or net dividends, reinvestment
  method, adjustment method of the provider, whether adjusted history is restated.
- **EUR conversion, €STR/EONIA, calendars, timezones.** All sourced and documented.
- **Gaps and anomalies.** Missing sessions, duplicates, stale runs, outliers and splits are
  listed and resolved by a documented rule. Unresolved gaps fail closed.
- **Manifest.** One JSON manifest (canonical JSON, SHA-256 of itself and of every raw file)
  with the universe, research series, executable products, splice points, anchors, calendars,
  partition boundaries and the cost survey. Its hash is part of every run record.

The audit may compute data-quality statistics (counts, gaps, extreme moves) but no moving
averages, signals, strategy returns or benchmark performance. If any requirement is not met,
T1 stops and reports.

## 8. Costs, capital grid and accounting

### 8.1 Cost model (EUR)

- **Commission.** IBKR Ireland rates for the relevant venues: percentage of value and minimum
  per order (and maximum where applicable). The values, plan (fixed/tiered), venue fees and the
  date and source of the survey are recorded in the manifest.
- **Spread.** Half-spread per side, per product, from a documented free source or a
  conservative documented estimate.
- **Slippage.** Per product, per side, documented.
- **TER.** Accrued daily as `TER / 365` per calendar day, with no double counting. A
  research series that is the executable product's own price/NAV already contains its TER
  (`TER_eu`). A US ETF proxy already contains its own TER (`TER_us`, recorded in the
  registry); its daily return is adjusted by `(TER_us − TER_eu) / 365` per calendar day so that
  the realistic scenario carries exactly the executable product's TER (owner decision at T1).
  An index series would contain no fee. The adjustment per scenario is in 8.2.
- **Currency.** Products trade in EUR; no FX conversion fee. Underlying currency exposure is
  captured by the EUR conversion of the research series.
- **Taxes** are out of scope and stated as such.

### 8.2 Scenarios

- `zero` — no commission, spread or slippage; returns gross of TER (diagnostic only).
- `realistic` — the documented values; one TER in total.
- `double` — commission (rate, minimum and maximum), spread and slippage doubled; two TERs in
  total.

| Series type | `zero` | `realistic` | `double` |
|---|---|---|---|
| Executable product's own series (contains `TER_eu`) | add back `TER_eu` | 0 | deduct `TER_eu` more |
| US ETF proxy (contains `TER_us`) | add back `TER_us` | add back `TER_us`, deduct `TER_eu` | add back `TER_us`, deduct 2 × `TER_eu` |
| Index series (contains no fee; none in the T1 universe) | 0 | deduct `TER_eu` | deduct 2 × `TER_eu` |

### 8.3 Capital grid

The same decisions (states and target weights) are replayed at 500, 2 000, 5 000, 10 000 and
25 000 EUR. Executed quantities differ through whole-unit rounding and minimum commissions.

For each capital, report annual trading cost (commission + spread + slippage) as a percentage
of average capital, and TER drag separately. The **minimum viable capital** is the smallest
grid value whose annual trading cost is below 0.50 % of capital.

The **reference capital** for the approval gates is the minimum viable capital measured on the
development + validation partitions, frozen at T4 before the sealed run. If no grid value
qualifies, the work stops and is reported; there is no fallback capital.

Full results at 2 000 and 5 000 EUR are always reported as well, for information only; they
never affect the verdict.

### 8.4 Accounting identities

- `NAV = cash + Σ quantity × price` at every mark.
- `ΔNAV = Σ asset price P&L + cash interest − commissions − spread − slippage` (TER inside the
  price series).
- Asset P&L contributions sum to total P&L minus cash interest.

## 9. Benchmarks (same costs, same calendar)

- **Equal weight, always invested:** `1/N` in every universe asset.
- **Global 60/40:** 60 % global equities (MSCI ACWI) and 40 % euro-area government bonds
  (the `EZ_GOV` asset), approved by the owner at T1. **Annual rebalance**, decided at `D` of
  the last master-calendar day of each year.
- **EUR cash:** section 6.5.

Both invested benchmarks start each partition in cash and pay their initial purchases, exactly
like the strategy. Their initial purchase is decided at `D` of the partition's first session;
equal-weight rebalancing is decided at `D` of the last master-calendar day of each month, the
60/40 rebalancing at `D` of the last master-calendar day of each year. Both fill
at the next close (section 6.4), with the same whole-unit, cost and cash rules.

## 10. Partitions and the sealed test

- The evaluable common history (after warm-up) is split chronologically: first 60 %
  development, next 20 % validation, last 20 % sealed test. Boundaries are rounded to month
  ends and written to the manifest before any run.
- Each partition is replayed independently, starting fully in cash on its first session.
  Data before the partition may warm up the 200-session SMA and the 60-session volatility;
  it contributes no returns. Initial entries pay costs. The benchmarks follow the same rule.
- **Secondary evidence (pre-registered, no effect on the verdict).** After the sealed claim,
  one continuous replay over the full evaluable history (starting in cash) reports the same
  one-sided Sharpe interval as section 12.
- The data loader refuses rows at or after the sealed boundary unless a valid sealed claim
  exists.
- **Sealed guard** (`app/trend`, same pattern as the M5 guard): a boundary file created with
  exclusive creation; a single permanent claim created by an explicit command that records the
  protocol hash, code commit, manifest hash and reference capital. No reset or release API. A
  crash after claiming still consumes the test.
- Development and validation results may be inspected freely in T3. Nothing in the protocol
  may change after T4 freezes it.

## 11. Metrics and uncertainty

For every run and scenario: total and annualised return (CAGR), excess over EUR cash,
annualised volatility, Sharpe (daily excess over EUR cash, sample stdev, annualised with the
master calendar's empirical sessions per year), maximum drawdown and its duration, turnover,
number of trades, average exposure, worst month, P&L by asset, cost by component.

Uncertainty: moving-block bootstrap from `app.validation.resampling`, block length 21
sessions, 2 000 resamples, fixed seed 20261114; paired resampling for strategy vs benchmark
differences (Sharpe difference, drawdown difference). Descriptive intervals are two-sided
95 %. The verdict uses a **one-sided 95 % lower bound** of the Sharpe ratio in excess of EUR
cash: the 5th percentile of the finite bootstrap Sharpe values.

## 12. Approval gates (sealed test, realistic costs, reference capital)

All must hold:

1. CAGR above EUR cash CAGR.
2. Sharpe ≥ 0.40 **and** above the equal-weight benchmark's Sharpe.
3. Maximum drawdown ≤ 20 % **and** smaller than the equal-weight benchmark's.
4. Cumulative return positive in the `double` scenario.
5. At least 3 of 4 equal chronological sub-periods (by sessions) with non-negative net return.
6. No asset contributes more than 40 % of the sum of positive net asset P&L contributions
   (fails if that sum is not positive).

Plus reproducibility: the sealed run reproduces exactly from the frozen manifest and commit.

### Verdict

- **EDGE** — all six gates pass and the one-sided 95 % lower bound of the excess Sharpe
  (section 11) is above zero.
- **INSUFFICIENT EVIDENCE** — all six gates pass but that lower bound is at or below zero; or
  the sealed test cannot be evaluated as pre-registered (data or integrity failure).
- **NO STATISTICAL EDGE FOUND** — any gate fails.

The same one-sided bound over the full history (section 10) is printed next to the verdict
as secondary evidence and never changes it.

Consequences, fixed in advance:

| Verdict | Consequence |
|---|---|
| EDGE | May proceed to a demo (IBKR paper) stage, under its own design and approval |
| INSUFFICIENT EVIDENCE with all six gates passed | May proceed to demo; any decision about real money is deferred |
| INSUFFICIENT EVIDENCE from an evaluation failure | Work stops and the failure is reported |
| NO STATISTICAL EDGE FOUND | Work ends |

The verdict is reported without softening. No verdict is proof or a promise of profit.

## 13. Mandatory tests

- **Causality:** mutating, deleting or appending any data after `T` leaves every state, weight,
  order, fill, cost and NAV up to `T` identical.
- **Next close:** weekends, holidays, different calendars per asset, decisions with no future
  close, master-calendar days where only some assets trade, pending orders not duplicated.
- **Signal boundaries:** 199/200 closes, equality `P = SMA` → `CASH`, `INSUFFICIENT_DATA` never
  trades.
- **Sizing:** volatility cap, `Σ w ≤ 1`, drift threshold exactly at 25 %, whole units,
  zero-quantity orders suppressed, sells before buys, no negative cash.
- **Accounting:** the identities of 8.4 on every day; one fully hand-calculated case (two
  assets, a few days, one entry, one drift rebalance, one exit, minimum commission, cash
  interest) with expected numbers written in the test.
- **FX and cash:** stale ECB rate fails closed; EONIA→€STR splice.
- **Reproduction:** two runs from the same manifest produce byte-identical outputs; a changed
  raw file is detected by hash.
- **Sealed guard:** refuses without boundary, refuses a second claim, refuses when hashes
  differ, survives a simulated crash as consumed.

Static checks: `ruff` and `mypy` clean on `app/trend` and its tests.

## 14. Package layout (indicative)

```text
app/trend/
  data.py        # loading raw files, manifest verification, EUR conversion, calendars
  manifest.py    # manifest schema, hashing (app.core.integrity)
  signal.py      # pure: SMA200 state
  allocator.py   # pure: vol, target weights, drift rule
  costs.py       # pure: commission/spread/slippage/TER per scenario
  simulator.py   # causal replay, fills at next close (section 6.4), cash, accounting
  benchmarks.py  # equal weight, 60/40, cash
  metrics.py     # metrics + bootstrap via app.validation.resampling
  sealed.py      # boundary + one-time claim (holdout_guard pattern)
  report.py      # trade-by-trade explanation, Portuguese owner report
  cli.py         # audit / dev / validation / sealed commands
tests/unit/trend/
```

Names may change; the economic rule, protocol and gates may not.

## 15. Phases

| Phase | Deliverable | Stop condition |
|---|---|---|
| T0 | This document + ADR-0040 + CHANGELOG. No code. | Approved 2026-11-14 with corrections |
| T1 | Universe proposal with justification; data audit; frozen manifest; cost survey | Owner approval; stop if free data is insufficient |
| T2 | `app/trend`: signal, allocator, cost simulator, causal replay; tests of section 13; ruff/mypy clean | Owner approval |
| T3 | Development and validation runs; Portuguese report with metrics, costs, capital grid and trade-by-trade explanation | Owner approval; no protocol change after this without a new design |
| T4 | Freeze protocol + code (commit + hashes); run the sealed test once; final report with verdict | End of delivery |

## 16. Declared limitations

- Proxies (often US-listed ETFs or indices) differ from UCITS products in timing (US open vs
  European open), dividend withholding and fees; the report quantifies what can be quantified.
- Free data may be restated by providers; the manifest freezes the files used.
- The sealed test covers roughly 3–4 years: a short sample with wide uncertainty.
- Owner and builder know market history; the universe choice is protected only by procedure.
- Results exclude taxes and assume IBKR fees as surveyed on one date.

## 17. Owner decisions at T0 (2026-11-14)

1. **Verdict mapping — approved, with additions.** (a) The interval is the one-sided 95 %
   bound of the Sharpe ratio in excess of EUR cash. (b) The same interval over the full
   history is pre-registered as secondary evidence with no effect on the verdict.
   (c) Consequences: `EDGE` may go to demo; `INSUFFICIENT EVIDENCE` with all six gates passed
   may go to demo with the real-money decision deferred; `NO STATISTICAL EDGE FOUND` ends the
   work. Sections 10, 11 and 12.
2. **TER in the `double` scenario — approved, without double counting.** The executable
   product's own series deducts one more TER; an index series deducts two. Sections 8.1, 8.2.
3. **Reference capital — approved.** Results at 2 000 and 5 000 EUR are also reported, for
   information only. If no grid value is below 0.50 % annual trading cost, stop and report.
   Section 8.3.
4. **Partitions start in cash — approved.** Earlier data may warm up the 200-session SMA and
   the 60-session volatility. The benchmarks also start in cash and pay their initial
   purchases. Sections 9, 10.
5. **Execution — decided now, one rule for every asset:** fill at the close of `T+1`. Open
   prices are not used. Section 6.4.
6. **Branch:** `feature/multi-asset-trend`, created from `main` before T1.

### T1 clarifications (2026-11-14, before any result; see owner decisions below)

- **Universe:** 11 assets in `app/trend/registry/universe.json`, all executable on Xetra in EUR.
  Research series rule: the product's own Yahoo series if it starts on or before 2008-12-31,
  otherwise the US-listed ETF/ETN tracking the same (or closest) index.
- **Global 60/40:** 60 % global equities (MSCI ACWI) + 40 % euro-area government bonds (the
  `EZ_GOV` asset): the balanced portfolio of a EUR investor.
- **Cash timing:** interest from `T-1` to `T` uses the €STR (or EONIA) for `T-1`, which is
  published on the morning of `T`.
- **Total-return convention:** daily factor `(close_t + dividend_t) / close_{t-1}`; the
  provider's adjusted close is used only to verify the data.
- **Sessions:** an asset's sessions are the provider's rows; exchange-calendar differences are
  listed by the audit.
- **Costs:** commission IBKR Pro Fixed (Xetra) 0.05 %, minimum EUR 3, no maximum, from
  secondary sources; spreads and slippage are documented estimates by liquidity class.

## 18. Owner decisions at T1 (2026-11-14, before any result)

1. **Commission:** IBKR Pro Fixed kept (0.05 %, minimum EUR 3), confirmed by the owner against
   the IBKR Ireland price list. Source and date are recorded in the registry and the manifest.
2. **Spread and slippage:** the class estimates are approved; each asset's tier is recorded in
   the manifest (`costs.asset_tiers`). Real values are measured in the demo phase.
3. **Global 60/40:** approved, with annual rebalance, start in cash and the same costs as the
   other benchmarks (section 9).
4. **Series rule:** approved with three conditions.
   (a) If no US-listed ETF or ETN tracks the product's own index, the asset is excluded; it is
   never replaced by a similar index (wording amended by the owner to admit ETNs). Enforced mechanically: a proxy's `tracks` must equal the product's
   `tracks` (`series_rule_violations`); the replay refuses to run otherwise.
   (b) Signal and EUR conversion use only values known at `D(T)`, given that the US close is
   later than the European one (section 6.1, data rule `availability`).
   (c) The TER difference between the US ETF and the European product is adjusted
   (sections 8.1, 8.2, data rule `ter`).
5. **Asset list (approved, second round).** `JAPAN_EQ` and `EM_EQ` violated condition (a)
   (IMI products against non-IMI US proxies). They move to the non-IMI products IQQJ
   (IE00B02KXH56, MSCI Japan) and IQQE (IE00B0M63177, MSCI Emerging Markets), whose own EUR
   Xetra series start in 2008, so the series rule uses them directly. Cost class B for both was
   fixed and committed before their download. `COMMOD` keeps DJP, an ETN on the same index;
   the known limitations state that an ETN's market price can depart from the index value. The
   other eight assets are approved unchanged. A pull of `origin/main` into this branch precedes
   T2.

**Manifest amendments.** The manifest is amended, never rebuilt, and `amendments[]` chains the
hash of each replaced manifest:

- `93d4e409…` → `5f50f2ae…`: decisions 1–4 (rules, costs, tiers); data, window and split
  unchanged.
- `5f50f2ae…` → `7276157c…`: product swap. IQQJ and IQQE files added, EWJ and EEM files dropped;
  every kept file byte-identical; data end unchanged; audit PASS; evaluable start, length and
  60/20/20 boundaries unchanged (only the per-asset warm-up dates in `window` changed).
- `7276157c…` → `d945583a…` (T2, section 19, approved by the owner): unit-price
  anchors. The Xetra series of the six
  proxy assets' products were added as anchor files only, and `unit_anchors` was recorded. No
  file changed; window and split unchanged.

Amendments are allowed only before the first result.

## 19. T2 implementation choices (2026-11-14, before any result; reviewed by the owner)

These make sections 6 to 12 executable. None uses a result; all are covered by tests.
Owner review (2026-11-14): choices 1, 2, 3, 4, 5 and 7 approved as written; choice 6
corrected and choice 8 strengthened as below; the unit-price anchor amendment approved.

1. **One set of decisions per run, from the realistic series.** States, volatility and target
   weights come from the realistic-scenario EUR total-return series. A cost scenario changes
   only unit prices (its TER adjustment) and trading costs. So `zero`, `realistic` and `double`
   isolate the cost effect of the same decisions.
2. **Unit prices (section 6.4).** `price_s(t) = unit_scale × L_s(t)`, where `L_s` is normalised
   to 1 at the asset's last session on or before the evaluable start. `unit_scale` is a
   manifest constant. Own series: the product's own close on that reference session. Proxies:
   the product's first Xetra close on or after the evaluable start, divided by the proxy's
   realistic level that day. All six anchor dates fall in the development partition
   (2009-10-08 to 2018-05-03), so no validation or sealed data enters the scale.
3. **Loaded history.** Every run loads each asset from 201 own sessions before the evaluable
   start up to the run's end; the windows are backward-looking, so values do not depend on
   that start.
4. **Rebalance days of the benchmarks.** "Last master-calendar day of the month (year)" is
   decided with the published exchange calendars (XNYS ∪ XETR, known in advance): `T` is the
   period end if no scheduled session follows it in the period. If that day is missing from
   the master calendar, the first master day of the next period takes the rebalance (decided
   from the past only).
5. **Fills.** On each day, sells fill before buys, each in asset-id order. A buy is cut to the
   whole units the cash pays for, after costs. The cut remainder is dropped, not carried. A buy
   that cannot pay for one unit is dropped and logged.
6. **Exits never depend on volatility (corrected by the owner).** An asset in `CASH` with an
   open position always gets its sell order, whatever its volatility. Unusable volatility
   (undefined, zero, or a stale window with more than 5 consecutive zero returns among the
   last 60) blocks only buys and position changes of a `LONG` asset: no order that day, the
   run is flagged, and the asset's last state is not updated. Test:
   `tests/unit/trend/test_strategy.py`.
7. **Annualisation.** Sharpe uses `(sessions − 1) / years` periods per year, with
   `years = calendar days / 365.25`. The EUR-cash return of each day uses the previous day's
   rate (ACT/360), as in section 6.5.
8. **Sealed guard and publication order (strengthened by the owner).** The guard files live
   in `data/trend/guard/` (outside Git); copies go to the registry and to Git.
   - T3 start: the boundary is created and its copy
     `app/trend/registry/sealed_boundary.json` is committed before the first development run.
   - T4, in this order: (1) create the sealed claim; (2) commit and push the receipt
     `app/trend/registry/sealed_claim.json` (protocol hash, code commit, manifest hash,
     reference capital, UTC date and time); (3) only then read sealed data. The data loader
     enforces (3): it refuses sealed rows unless the registry receipt and the receipt on the
     upstream branch both equal the claim (`guard.require_published_claim`).
