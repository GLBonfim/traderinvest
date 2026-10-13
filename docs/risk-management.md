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

(Full 6 × 6 × 3 table: `uv run python -m app.risk.cli run --symbol SPY` → `data/risk/<run_id>/risk_table.csv`; see [Running the suite](#running-the-suite-full-or-selective).)

Paired block-bootstrap differences vs control — **exploratory** percentile intervals of the
realized returns, not validated confidence intervals (see *Statistical outputs* below and
[statistical-validation.md](statistical-validation.md)) (full slice, Phase 9 resampler, 95%,
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

## Running the suite (full or selective)

```bash
# full suite, as before: every strategy x every scenario x every slice, cost D
uv run python -m app.risk.cli run --symbol SPY [--out-root data/risk | --out DIR]

# selective: ONLY the listed cases
uv run python -m app.risk.cli run --symbol SPY --strategy sma_trend \
    --scenario control_no_overlay fixed_fraction_50 drawdown_lock_20 atr_stop_3 \
    --cost D --slice full early late [--no-compare]
```

**Selection** (`app/risk/research.py`, validated before any data is read or anything is
written; an invalid selection exits with code 2):

| Option | Values | Notes |
|---|---|---|
| `--strategy` | `buy_and_hold`, `sma_trend`, `sma_crossover`, `rsi_momentum`, `price_action_trend`, `regime_trend` | one or more; only these strategies are computed |
| `--scenario` | the six pre-declared scenarios | one or more; must include `control_no_overlay` (the suite measures every overlay against it) |
| `--cost` | `A_zero`, `B_5bps`, `C_10bps`, `D_default` (or `A`–`D`) | exactly one ([statistical-validation.md](statistical-validation.md)); `D` = the Phase 8 default |
| `--slice` | `full`, `early` (≤ 2009-12-31), `late` (≥ 2010-01-01) | one or more |
| `--no-compare` | — | skip the bootstrap comparisons (selective runs only) |

- **No implicit widening.** Without any of `--strategy/--scenario/--cost/--slice` the full
  suite runs (previous behaviour, cost D). As soon as one is given, **all four are required**;
  a partial selection is refused, never completed with defaults. Unknown, duplicate or empty
  values are refused. Comparisons need the control plus at least one overlay.
- The run is checked against the selection: a result outside it fails the run.
- Scenarios and slices run in their pre-declared order (control first), whatever the order typed.

**Output.** Every run writes into a NEW directory: `--out DIR` (exact path) or
`<out-root>/<run_id>` (default `data/risk/risk-<UTC timestamp>Z-<8 hex>`). The directory is
created with one atomic `mkdir`; an existing path — even an empty directory — is refused (exit
code 3) and left untouched. Result files are created exclusively (never overwritten); nothing
is deleted or cleaned. Contents: `manifest.json`, `risk_table.csv` and, with comparisons,
`risk_comparisons.csv`. Exit code 0 only when the run is complete; 1 on failure.

**Database access: read-only, verified** (`app/database/readonly.py`). `PGOPTIONS` cannot be
used — the application passes its own libpq `options` (`-c timezone=UTC`), which takes
precedence over the environment variable (verified: with `PGOPTIONS=-c
default_transaction_read_only=on` the application connection still reported `off`). The CLI
loader therefore uses:

1. a dedicated, short-lived engine (`read_only_engine()`: NullPool, no pre-ping, disposed after
   the load; the application's shared engine and pool are not touched) whose connections carry
   `postgresql_readonly=True`. With psycopg 3 this sets `Connection.read_only = True` when the
   connection is created, so psycopg opens **every** transaction on it with `BEGIN READ ONLY`
   (PostgreSQL then rejects any write) — also after a commit or rollback, and on any other
   connection of this engine;
2. a `before_cursor_execute` guard on that engine, run before **every** statement: only
   `SELECT`/`SHOW` are allowed; the DBAPI connection's `read_only` must be `True`; and, on the
   same cursor and in the same transaction, `SHOW transaction_read_only` must return `on`
   immediately before the statement is sent. The first statement of a transaction therefore
   opens it as `BEGIN READ ONLY` and has it verified before any data is read;
3. an explicit `SHOW transaction_read_only` check when the session opens, before the loader
   (`load_backtest_bars`: instrument id, OHLCV, adj_close — three SELECTs in one session) runs.

Any failed check (`off`, no answer, an error, a non-read statement, a DBAPI without read-only
mode) raises before the statement is sent: no data is loaded, the suite does not start, the
run is `failed` (exit code 1) and the manifest records the reason in `db_access.violation`.

The manifest's `db_access` holds the evidence: `mechanism`, `read_only_confirmed` (true only if
at least one statement ran and every statement was preceded by `on`), `statements_checked`,
`checks`, `transaction_read_only_values` and `violation`. A loader that supplies no evidence
(in-memory bars, tests) is recorded with `read_only_confirmed: false`; a load result whose
access is not confirmed is never used.

Unit tests use a psycopg-like fake. On the real server (2026-10-13, development database) the
dedicated engine's connection reported `read_only = True`, the psycopg transaction start
command `BEGIN READ ONLY`, `SHOW transaction_read_only = on` (with the session default
`default_transaction_read_only = off`, i.e. the protection comes from the transaction), no
transaction id assigned, and a non-read statement refused before it was sent.

**Manifest** (`manifest.json`, schema `risk-research-run/2` — runs recorded before this
change carry `risk-research-run/1` and are not rewritten — written atomically at start and at
the end): `run_id`; `status` (`running` → `complete` | `failed`; **only `complete` is a
result** — a run interrupted or unable to write its final manifest stays `running`);
`started_at`/`completed_at` (UTC); `code` (app version, git commit, branch, dirty flag);
`environment` (`numpy_version`: the bootstrap uses numpy's PCG64 generator);
`statistics` (what the point estimates and intervals are, the order-dependent metrics and
the sparse-event threshold);
`command`; `selection`; `db_access` (above); `config` (risk version; each selected scenario's full configuration and
fingerprint; the cost scenario; the effective backtest configuration and fingerprint; slices;
strategy-engine fingerprint; bootstrap parameters, `validation_config_fingerprint` — the
existing `ValidationConfig.fingerprint()` of the configuration object passed to the suite,
covering all its fields including slices and cost scenarios the risk comparisons do not
use — `family`, `interval_kind`, `compared_metrics`, `order_dependent_metrics`); `input` (symbol, source, rows, first/last
bar, data fingerprint); `results` (run and row counts, metric columns, comparisons, the
sessions actually simulated per slice, timings, `intervention_events`,
`estimates_outside_own_interval`); `artifacts` (path, SHA-256, bytes); `error`.
No settings, environment values or credentials are recorded.

**Data fingerprint** (`bars-canonical-v1`): SHA-256 over UTF-8 text — a header line
`bars-canonical-v1|ts|open|high|low|close|volume|adj_close`, then one line per bar in index
order: the timestamp converted to UTC in ISO-8601, then each column as float64 in Python `repr`
(shortest round-trip, platform-independent; NaN → `null`), joined by `|`, each line ending in
`\n`. Bars must be unique and strictly increasing. It identifies exactly the bars handed to the
suite (any value, bar or timestamp change alters it; column order and index time zone do not).
Limitations: it covers these six columns only (not the database row ids, `is_closed` — only
closed bars are loaded — or ingestion metadata); `-0.0` and `0.0` hash differently; database
`NUMERIC` values are hashed after conversion to float64, as the suite uses them.

The selective mode does not change any formula, scenario or slice; it only chooses which
pre-declared cases are run and records how.

**Statistical outputs (exploratory).** `risk_comparisons.csv` keeps its original columns and
order (`slice … seed`) and appends: `bootstrap_median` (median of the finite bootstrap
differences — far from `estimate` means a biased distribution), `n_finite` (finite samples used;
non-finite ones are excluded, never imputed), `order_dependent` (true for `max_drawdown`),
`interval_kind` (`exploratory_block_bootstrap_percentile`), `estimate_in_interval` (false when the
point estimate falls outside its own interval; empty when undefined) and `note` (the
order-dependence caveat on max-drawdown rows). No p-values and no multiple-comparison correction
are computed for the risk overlays; the intervals must not be read as tests.

**Sparse intervention events (descriptive warning).** For each event-driven control in a
scenario (stop → `stop_triggers`, drawdown lock → `drawdown_lock_events`; a session lock has no
counter and is reported `not_counted`), `risk_table.csv` appends `event_levels`, `event_counts`
and `event_warning`, and the manifest lists `results.intervention_events`. Levels: `none` (0
events: the control's effect is not observed in that window — not evidence that it is
ineffective), `single` (1), `few` (below the threshold), `several` (at or above it);
`not_applicable` for scenarios without an event-driven control (sizing and limits act
continuously). The threshold is a documented convention (`SPARSE_EVENT_THRESHOLD = 5`, recorded
in `statistics.sparse_event_threshold`, configurable through `execute(...,
sparse_event_threshold=...)`), not a statistical rule.

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
