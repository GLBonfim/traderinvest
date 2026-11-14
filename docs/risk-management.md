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

- `max_gross_exposure`, `max_single_instrument_exposure` (≤ 1), optional `max_position_notional`
  — which must exceed 1e-12 × the initial equity (below it no position could ever be held;
  refused when the risk manager is built, and an explicit error if the equity later grows past
  it).
- **Drawdown**: the state is decided by comparing the equity at the close with the LEVEL
  (ADR-0034 D-A2): LOCKED iff equity ≤ HWM × (1 − lock), WARNING iff equity ≤ HWM × (1 −
  warning) (HWM starts at initial capital); equality activates, no tolerance. The reported
  drawdown stays equity / HWM − 1 (it may read −0.19999999999999996 on a locked close). While
  LOCKED: `force_flat` exits and blocks entries, `block_entries` keeps the position and blocks
  increases. The lock covers `cooldown_sessions` decisions including the triggering close; a
  further fall neither extends nor restarts it; the next close unlocks and resets the HWM to
  the current equity (otherwise a flat account could never recover and would stay locked
  forever) — except at equity 0, where the last positive HWM is kept and the close relocks.
  The reported `max_drawdown` metric keeps the Phase 8 definition (global HWM, never reset).
- **Session loss** (infrastructure, not enabled in any scenario): equity(T)/equity(T−1) − 1
  ≤ −limit blocks increases for N sessions; consecutive losing round trips ≥ K blocks entries.

## Stops on daily OHLC

> Semantics of the stop, the drawdown lock and their events:
> [ADR-0034](decisions.md#adr-0034--risk-mechanism-semantics-stop-drawdown-lock-and-their-events)
> — implemented in the working tree, **not yet committed**. Invalid financial state (D-A5): a
> non-finite or negative equity, or a non-finite or non-positive high-water mark, raises
> `ValueError` (the run fails); equity 0 is valid: it locks, normalised exposure is 0, no entry
> or increase is approved (reason `zero_equity_no_increase`, a sizing-type reason, never a
> stop or an exit cause), the session loss after a zero equity is not computed (`None`) and
> keeps the session protection. Undefined daily returns: see *Daily net returns* below.

- Level set once at entry: fixed % below the entry fill, or entry − k·ATR14 (ATR known at the
  entry decision). With an ATR stop, an entry whose decision ATR is undefined (NaN, ±inf, 0 or
  negative) is **refused** — approved at 0 with reason `stop_input_undefined` (ADR-0034 D-A3);
  an open position is unaffected (its level was fixed at entry); `fixed_pct` needs no ATR.
- **Triggered only when the session close ≤ level**; the exit is decided at that close and
  executed at the next open (gap risk borne).
- **Daily OHLC never reveals intraday order.** A low ≤ level with a close above is reported as
  `stop_intraday_touch` and is **not** an exit; a bar whose range spans both far above and far
  below the level is treated the same way. Intraday stop orders are **unsupported** (they would
  require intraday data).
- **Condition vs activation (ADR-0034 D-A1).** `stop_triggered` is the condition (close ≤
  level while holding) and may repeat while an exit is pending or rejected; `stop_activation`
  is the **first** condition of the position episode. The episode state persists until a
  confirmed exit fill and is reset by an entry or exit fill; a rejected exit is not a fill.
- Stop and strategy exit on the same close → one activation and one exit at the next open.
- **Exit attribution.** Every exit records `exit_causes`: every cause present, unique, sorted
  and joined by `+` (`drawdown_lock`, `sizing`, `stop`, `strategy`; `session_lock` reserved),
  e.g. `stop+strategy`; never empty. While an activated stop's exit is unconfirmed (pending
  or rejected), every new exit attempt keeps `stop` among its causes (e.g. the strategy turned
  FLAT meanwhile: `stop+strategy`). `exit_reason` keeps its meaning (the risk decision reason
  of the exit).
- After a stop, re-entry is blocked until the strategy state is **FLAT** (`INSUFFICIENT_DATA`
  never releases it; `RiskManager.decide` receives `strategy_state` explicitly, D-A4),
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

> These SPY results were computed **before ADR-0034** and have not been recomputed (no
> operational backtest was run). D-A3 refuses entries during the ATR warm-up, so ATR-stop
> rows may differ; D-A2 changes a lock only when equity equals the level exactly.

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
is deleted or cleaned. Contents: `manifest.json`, `risk_table.csv`, `daily_returns.csv` and,
with comparisons, `risk_comparisons.csv`. Exit code 0 only when the run is complete; 1 on
failure.

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

**Manifest** (`manifest.json`, schema `risk-research-run/3` — runs recorded earlier carry
`risk-research-run/1` or `/2` and are never rewritten; `/3` only adds
`results.daily_returns` and the returns artifact — written atomically at start and at
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
`estimates_outside_own_interval`, `daily_returns`); `artifacts` (path, SHA-256, bytes);
`error`.
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
are computed for the risk overlays; the intervals must not be read as tests. A comparison
involving a series with undefined daily returns (see *Daily net returns*) is **not testable**:
all comparisons of a strategy when its control has one, only the overlay's own otherwise (the
others are computed on the full sessions). Its row is kept with NaN estimate and interval,
`n_finite` 0, empty `estimate_in_interval` and a `note` starting `not testable`; no session is
dropped and no value imputed.

**Return-metric status (`risk_table.csv`).** An empty `annualized_volatility`, `sharpe` or
`sortino` cell can have two causes, so the table appends `annualized_volatility_status`,
`sharpe_status` and `sortino_status`, set from the calculation layer (never from the cell):

| Status | Meaning |
|---|---|
| `defined` | a finite value (zero included: a zero volatility is defined) |
| `undefined_returns` | not computed: the run's series has undefined daily returns (the in-memory value is `None`, with `undefined_returns` and `return_metrics_note` in the run's metrics) |
| `undefined_nan` | NaN in the metric's own domain: fewer than 2 sessions, zero standard deviation (Sharpe), no negative return (Sortino) |

One column per metric because the causes are independent (e.g. Sortino NaN while Sharpe is
defined). The existing columns, their order and values are unchanged; the status columns are
appended after every existing run column (diagnostics included) and before the event columns. A state the calculation never produces
(None without undefined returns, a number although there are, an infinite value) fails the
run instead of being classified.

Which of these comparisons may ever be confirmatory, and how, is fixed in advance in
[statistical-protocol.md](statistical-protocol.md) (draft, not yet approved).

**Sparse intervention events (descriptive warning).** For each event-driven control in a
scenario (stop → `stop_activations`, one per position episode; drawdown lock →
`drawdown_lock_events`, transitions of the drawdown monitor into LOCKED; session lock →
`session_lock_events`, ADR-0034 D-A8; `stop_triggers` still counts stop conditions), `risk_table.csv` appends `event_levels`, `event_counts`
and `event_warning`, and the manifest lists `results.intervention_events`. Levels: `none` (0
events: the control's effect is not observed in that window — not evidence that it is
ineffective), `single` (1), `few` (below the threshold), `several` (at or above it);
`not_applicable` for scenarios without an event-driven control (sizing and limits act
continuously). The threshold is a documented convention (`SPARSE_EVENT_THRESHOLD = 5`, recorded
in `statistics.sparse_event_threshold`, configurable through `execute(...,
sparse_event_threshold=...)`), not a statistical rule.

**Daily net returns** (`daily_returns.csv`, schema `risk-daily-returns/2` for new runs;
`risk-daily-returns/1` for runs written before ADR-0034 step 4b, `app/risk/artifacts.py`). The per-session series each run actually produced, taken from
`RiskRun.equity` during the run (no extra simulation, never rebuilt from aggregate metrics), one
long table for every (slice, strategy, scenario) of the run:

`slice,strategy,scenario,cost_scenario,session,ts,equity,daily_return,position,costs`

- `session` = 0-based position in its series; `ts` = bar open time in the canonical form
  `YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00` (`pandas.Timestamp.isoformat()` of the UTC instant;
  naive values, `Z` and other offsets are refused even for the same instant); series in the
  run's canonical order, sessions strictly increasing; no missing sessions inside a series;
- `equity` = cash + shares × raw close after the open fill; `daily_return` =
  equity(t)/equity(t−1) − 1 with equity(−1) = the backtest initial capital — the net series the
  bootstrap comparisons use; the cost scenario's costs (commission, half spread, slippage) are
  deducted from cash at the fill; raw prices, no dividends; `position` = exposure at the close;
  `costs` = costs paid that session. A session without a position is an ordinary row (position
  0, return 0 unless costs were paid);
- floats in Python `repr` (lossless for float64); non-finite values written as `nan`, `inf`,
  `-inf` and never imputed (their count per series is in the manifest);
- **undefined returns (schema `/2`, ADR-0034 D-A5):** `daily_return` is undefined when
  equity(t−1) = 0 — and for every following session while the equity stays 0. It is written
  as the explicit token `null` (never `0`, `nan`, `inf` or an estimate; no other spelling is
  accepted) and counted per series in the manifest (`undefined_returns`). A close that
  *reaches* 0 from a positive equity is a defined `-1.0`. In memory the column is a nullable
  `Float64` with `pd.NA` (plain float64 when a file has no null). Schema `/1` has no null
  token: `/1` files keep their contract and a `null` in one is refused;
- published atomically and never over an existing file: written, flushed and fsynced to
  `<name>.tmp` (created exclusively — an existing `.tmp` is not ours and is never touched),
  hard-linked to the final name (refused if it exists), temporary removed. A failure before
  publication removes the temporary file when it can; if that cleanup fails too, the
  residue is reported as a note on the original error, which is not hidden. A failure to
  remove the temporary name after publication fails the run (fail-closed). The run is then
  `failed`, never `complete`. The directory is not fsynced, so the durability of the new
  name after a power loss is not guaranteed. Requires hard links (NTFS, ext4, APFS).

`results.daily_returns` records `path`, `schema`, `columns`, `convention`, `initial_capital`,
`cost_scenario`, total `rows` and, per series, slice / strategy / scenario / cost scenario,
`rows`, `first_ts`, `last_ts`, `non_finite_returns` (`nan`/`inf` only), `undefined_returns`
(schema `/2` only: null returns), `risk_fingerprint` and `backtest_fingerprint`; the file's SHA-256 and size are in `artifacts`, and the code and
configuration that produced it are the manifest's `code`, `environment` and `config`.

**Read-only verification.**

```bash
uv run python -m app.risk.cli verify --run data/risk/<run_id>   # exit 0 ok, 1 problems (JSON)
```

`app.risk.artifacts.verify_run` reads only, never writes or repairs, needs no database or
settings, and never raises: every problem — including a malformed manifest — is reported.

1. **Structure first**, per schema version, before any nested value is used: `/1` (selection,
   artifacts, results counts and windows, configuration fingerprints, slices, cost scenario),
   `/2` (+ `environment`, `statistics`), `/3` (+ `results.daily_returns` with its series
   list). Wrong types or missing fields are problems, not exceptions.
2. **Artifact names**: plain file names only (`^[A-Za-z0-9][A-Za-z0-9_.-]*$`); `manifest.json`,
   absolute paths, separators, `..`, symbolic links and anything resolving outside the run
   directory are refused before any file is opened.
3. **Files**: each artifact read once; size and SHA-256 checked on those bytes, which are the
   bytes later parsed. Required artifacts per version: `risk_table.csv`, plus
   `risk_comparisons.csv` when `selection.compare`, plus `daily_returns.csv` for `/3`; a
   `complete` run cannot pass with `artifacts: []`; artifacts not defined by the version,
   duplicated entries and unexpected files (e.g. a left `.tmp`) are problems.
4. **Results vs selection** (every version): `runs` = `table_rows` = selected cases;
   `comparisons` = slices × strategies × overlays × 4 metrics when comparing, else 0; the table
   and comparison rows are exactly the selected keys (comparisons against the control).
5. **Daily returns** (`/3`): header and `columns`; `convention`; canonical timestamps and
   lossless values on every row; unique keys, `session` 0..n−1 and strictly increasing `ts`;
   series = selection = manifest list (no duplicates); one cost scenario across selection,
   configuration, block, series and rows; `initial_capital` = the backtest configuration;
   per-series rows / first / last bar / non-finite count = manifest = `results.windows`;
   `risk_fingerprint` = the recorded scenario fingerprint; `backtest_fingerprint` = the
   fingerprint of the slice-specific backtest configuration rebuilt from the recorded one (the
   recorded base fingerprint must first be reproducible with the running code, otherwise the
   series fingerprints cannot be validated and verification fails); every finite return
   consistent with the recorded equity (relative tolerance 1e-12). Schema `/1` or `/2` with
   its own convention; `/2`: a null only where the previous recorded equity is 0, never a
   number there, and per-series null counts = manifest `undefined_returns`; `/1`: no null.

**Guarantee: self-consistency, not authenticity.** Sizes, hashes and cross-checks show that a
run is internally consistent and has not been damaged or edited in one place only. They do
not detect a coordinated edit of the manifest and its artifacts with recomputed hashes (e.g.
equity and return changed together). Detecting that would need the manifest's SHA-256 anchored
outside the run directory (for instance in a hash-chained registry), which is not implemented.
Historical `/1` and `/2` runs are checked against their own minimum requirements only.

**For later statistics.** `load_returns_matrix(run_dir, slice_name=…, strategy=…,
scenarios=[…])` returns the session × scenario matrix of net returns from a run that verifies,
parsed from the very bytes whose SHA-256 was checked (no verify-then-reread). It raises
`ReturnsMatrixError` (a `ValueError`) with the reason for an unverified run, an empty or
duplicated scenario list, a missing series, series not aligned session by session or a series
containing an undefined (`null`) return, which is not usable as a complete series; it never
aligns, imputes, drops or merges anything. Complete series load as float64, as before. Persisting returns makes comparisons
reproducible and auditable; it does not by itself validate any statistical method applied to
them (no p-values, multiple-comparison corrections or new intervals are computed here).

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
