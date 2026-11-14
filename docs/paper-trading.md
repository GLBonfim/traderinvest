# Paper Trading (Phase 12)

> **Paper trading is a local simulation. It does not transmit orders to any brokerage or
> market.** There is no broker integration, no broker SDK, no network access from
> `app/paper/`, no credentials and no real-money execution path. Results are hypothetical
> fills under the Phase 8 assumptions; they are not evidence of profitability.

Code: `app/paper/` (`models.py`, `account.py`, `broker.py`, `trader.py`, `state.py`,
`ledger.py`, `store.py`, `engine.py`, `cli.py`) · paper version `1.0.0` · ADR-0021, ADR-0035.

## Architecture

```
completed bar T (local database; app/paper never fetches data)
      ↓  BarInput(T): open/low/close, strategy state, realized_vol_20, ATR14,
      ↓               observed_at = close(T), effective_at = next XNYS session open
Strategy state (Phase 7, copied verbatim)
      ↓  requested target (LONG 1 / FLAT 0 / INSUFFICIENT_DATA keeps the previous request)
RiskManager (Phase 11; single source of truth for sizing, limits, locks, stops)
      ↓  approved target + reasons
Instruction (risk-approved target only) ── persisted as PENDING ──┐
                                                                 ↓  open of the next session
PaperBroker: safety checks → plan_order/apply_order (Phase 11) → simulated fill at raw open
      ↓
Account (cash, quantity, cost basis, realized P&L, costs) → snapshot at close(T+1)
      ↓
append-only hash-chained ledger + atomic state file
```

One state machine, `PaperTrader.process(bar)`, drives both modes:

| Mode | Driver | Use |
|---|---|---|
| Historical replay | `PaperTradingEngine.replay_inputs` / `replay_baselines`, CLI `replay` | deterministic verification over a full history, reports |
| Incremental | `PaperStore.process` / `process_many`, CLI `run` | completed bars processed one at a time; durable, restart-safe, idempotent |

Because both use the same `process` and the same `BarInput` construction, processing a history
bar by bar (with restarts) produces exactly the replay's records, ledger events (including IDs
and hashes) and account state — tested on synthetic data and on the full SPY history.

### Per-session lifecycle (`process(T)`)

1. **Open of T** — if an instruction is pending (decided at the previous close), the broker
   executes it at the raw open of T. It is filled only if T is the scheduled session, T is after
   the decision, the target is in [0, max exposure], quantities are non-negative and cash stays
   ≥ 0; otherwise the order is `REJECTED` with a reason and the position is unchanged.
2. **Close of T** — account valued at the raw close; drawdown/session monitors updated; the
   stop is evaluated on the close (Phase 11: never on intraday ordering).
3. **Decision at the close of T** — requested target → `RiskManager.decide` → approved target →
   `Instruction` scheduled for `effective_at` (the next XNYS session open from the calendar,
   never "the next row"). The instruction is persisted as pending.
4. **Snapshot** — account snapshot at the close, including the pending kind just created.

A decision is never executed on the bar that produced it (`effective_at > observed_at` is
enforced; a broker check rejects any execution not after its decision). A decision on the last
processed bar stays pending until its session is processed — never executed on an invented bar.

### Window start (`start`, new accounts) — ADR-0035

`start` is the **first result session** of a new account (replay `start=`, CLI `replay/run
--start`, dashboard `paper_process_sessions(start=…)`). The convention is the research
engine's (`RiskOverlay.run(start_index=k)`), so a paper window and a research window with the
same `start` give the same sessions, fills and equity (tested):

1. **Causal requested target.** The strategy states of the sessions before `start` rebuild the
   requested target one session at a time (LONG 1, FLAT 0, `INSUFFICIENT_DATA` keeps the
   previous request; 0 before any valid state). Nothing after `start−1` is read.
2. **Decision at the close of `start−1`** (`PaperTrader.prime`): `RiskManager.decide` with
   equity = the initial capital. The risk monitors (drawdown, session) are **not** updated;
   the account is not valued at that close as a result session.
3. **Reference snapshot.** The decision is recorded with a snapshot of the account at that
   close: no position, cash = equity = initial capital, mark = raw close of `start−1`, the
   pending kind just created. Ledger and state schemas are unchanged; the pair keeps the
   per-session order *decision, snapshot*, so broker, alert and operations readers work as
   before. It exists for traceability and execution only.
4. **Entry at the open of `start`.** A LONG request at `start−1` (including one carried
   through `INSUFFICIENT_DATA`) is a **new** entry at the first window open, with the normal
   costs and its own stop (ATR of the `start−1` decision). No position, cost basis or stop
   is inherited from the strategy's history.
5. **Results start at `start`.** Equity, daily returns (the first one against the initial
   capital), metrics, portfolio rows and trades contain window sessions only; `start−1` is
   never counted in `session_seq` / `sessions_processed`. The replay's `decisions` frame (and
   `decisions.csv`) includes the `start−1` decision, as the research decision frame does;
   `portfolio.csv` does not include the reference snapshot.
6. **Boundaries.**
   - No session before `start` (or `start` on/before the first bar): no previous decision; the
     first decision is at the close of the first session (unchanged behaviour).
   - The session immediately before `start` missing from the data while window sessions exist:
     `ValueError("missing session in data before the window start")` (the research engine also
     refuses a missing session).
   - Incremental mode before `start−1` is available (latest bar earlier than `start−1`):
     nothing is processed; the decision is taken on a later run, once `start−1` is known. If
     `start−1` is known but `start` is not, the account holds only the decision and the
     reference snapshot, with the entry pending for `start`.
   - A `start` on a non-session day: the window begins at the next session; `start−1` is the
     last session before it.
7. **Existing accounts.** `start` applies only while an account has processed no session; any
   other account ignores it and its ledger and state are unchanged. The reference events use
   the normal commit protocol (ledger append, then state), including crash recovery.

**Distinguishing the reference snapshot.** No record field marks it. In the ledger it is
identical, field for field apart from IDs, times and the mark price, to a normal session of a
flat account whose decision schedules an entry; the whole ledger of an account started with
`start` equals, including hashes, that of a new account whose first session is `start−1`. Only
the state differs (`sessions_processed` is one lower), which is an account-level inference, not
a per-record one. The snapshot itself does not name a fill.

**Dashboard session types (D1, implemented).** The *Account snapshots* table of the dashboard
adds two display-only columns, `session_type` and `session_type_basis`, computed by
`classify_snapshots` (`app/dashboard/services/paper.py`) over the **whole committed ledger**
(not only the sessions shown) and the account's `sessions_processed`; a caption under the tab
states the limitation. Nothing is persisted: no ledger, state or schema field is added and no
ledger hash changes.

| Type shown | Rule |
|---|---|
| `trade` | a `fill` event exists in the **same session**; never inferred from position or equity changes |
| `no trade` | no `fill` event in the session. It does **not** exclude an order in that session: a rejected order (no fill) is shown as `no trade`; the rejection is visible in the *Orders* tab |
| `window-start reference (indicative)` | the account's first snapshot, without a fill, when the committed snapshot count is `sessions_processed + 1` and the snapshot is flat with the initial capital |
| `indeterminate` | the account's first snapshot without a fill when the counts are inconsistent, or one snapshot more than `sessions_processed` while the snapshot is not flat at the initial capital |

- `window-start reference (indicative)` is **not a persisted property and not a guaranteed
  per-record identification**: it is an inference from the account's state
  (`sessions_processed` in `state.json`) and the ledger, and the same ledger is classified
  differently for an account whose first session is `start−1` (shown as `no trade`).
- Only an account's first snapshot can be a window-start reference (ADR-0035), so later
  snapshots without a fill are `no trade`. A first snapshot classified `no trade` (counts
  equal) is a counted session by the same, indicative, inference; its basis says so.
- Tests: `tests/unit/dashboard/test_snapshot_types.py` (service level). The running Streamlit
  page (caption and columns as rendered) has **not** been validated.

Options to mark the reference per record, **not implemented or approved**:

| Option | Change | Per record | Schema |
|---|---|---|---|
| D2 | Additive snapshot field (e.g. `result_session: bool`), written for new events only | yes | ledger record schema (additive; old events and hashes unchanged; readers by key) |
| D3 | Distinct `snapshot_id` form for the reference (e.g. `<account_id>-R<YYYYMMDD>`) | yes | ID convention changed (field types unchanged) |
| D4 | Record the reference session in the account's `state.json` (or manifest) | no (account level) | state schema 2 (or manifest content) |

## Risk integration (no bypass)

`PaperBroker.execute` accepts only `Instruction` objects (anything else raises `TypeError`),
and the only producer of instructions is `PaperTrader`, which takes the target from
`RiskManager.decide`. Tests prove that a raw `LONG` state cannot become a fill: with the risk
decision patched to approve 0, a strategy that is `LONG` on hundreds of bars produces no order
and no fill. The strategy state is stored verbatim next to the requested and approved targets.

The only other source of targets is the private zero-cost "gross shadow" of a replay, which
re-plays the targets the risk layer already approved (to compute gross equity), as Phase 8/11
do.

## Pending instructions and rebalances

Classified at the decision close from the position held at that close:

| `pending_kind` | Condition |
|---|---|
| `none` | flat and target 0, or holding with \|exposure − target\| < band (0.10) |
| `entry` | flat → target > 0 |
| `exit` | holding → target 0 |
| `rebalance` | holding → target > 0 and \|exposure − target\| ≥ band |

The open of the next session decides what actually happens (prices move overnight: a
`none` can become a small rebalance and vice versa); the reconciliation record states the
actual outcome (`filled`, `rejected:<reason>`, `no_order_required:<why>`). At the end of a
replay, an `entry`, `exit` **or `rebalance`** still pending is reported as an order with status
`PENDING_NO_SESSION` (rebalances were previously not reported).

Fill actions: `entry` (flat → long), `add` (buy while long), `reduce` (partial sell), `exit`
(sell everything). A transition 0 → 0.5 → 0.8 → 0.3 → 0 is one round trip with four fills
(entry, add, reduce, exit), not four independent trades. Exposure > 1, short positions,
negative quantities and negative cash are impossible (rejected by the broker and validated on
every state load).

## Accounting

Average-cost method with transaction costs capitalised into the cost basis:

```
buy  (notional N, costs c):  cash -= N + c;  basis_notional += N;  basis_costs += c
sell (notional N, costs c):  cash += N - c;  fraction f = q_sold / q_before (1 on exit)
                             removed = f × (basis_notional + basis_costs)
                             realized_pnl += (N − f·basis_notional) − (f·basis_costs + c)
cost_basis      = basis_notional + basis_costs            (0 when flat)
market_value    = quantity × mark          mark = raw close of the session
equity          = cash + market_value
unrealized_pnl  = market_value − cost_basis               (0 when flat)
total_pnl       = realized_pnl + unrealized_pnl
cumulative_costs = Σ commission + spread + slippage of all fills
```

Identity (exact in real arithmetic; floating-point residue ≤ 1e-6 on a 100,000 account,
tested): `total_pnl = equity − initial_capital`. Costs are part of the P&L, not an extra term:
entry costs sit in the basis (unrealized while the position is open), exit costs reduce the
realized P&L. For an all-in/all-out round trip, realized P&L equals the Phase 8 trade
`net_pnl` = (exit notional − entry notional) − (entry costs + exit costs). Worked example
(zero costs, 10,000): buy 50 @100; add 30 @100; at 120 sell 51 → realized (6,120 − 5,100) =
1,020, basis 2,900; at 110 sell 29 → realized 3,190 − 2,900 = 290; total 1,310 = equity −
10,000 (unit test).

Round trips (flat → flat) are reported with the Phase 8 trade formulas: gross = Σ sell
notional − Σ buy notional, costs = Σ buy costs + Σ sell costs, net = gross − costs, net return =
net / (Σ buy notional + Σ buy costs).

**Execution-simulation safeguard (project convention, ADR-0021; approved at the Phase 12 gate).**
This is a property of the paper broker only — **not a change to the research backtester**:
Phase 8/11 behaviour and results are frozen and keep their original arithmetic.
**Only deviation from Phase 8/11 arithmetic.** `buy_notional` solves N + costs(N) = cash in
floating point and can leave cash at ≈ −1.5e-11 (exposure 1 + 2e-16). The paper broker never
books negative cash: it lowers such a buy's notional by a few ulps (at most `cash_tolerance` =
1e-6 currency, otherwise the order is rejected). Paper and Phase 8/11 equity therefore agree
within 1e-6 (asserted on all baselines, all six risk scenarios and the full SPY history);
dates, prices, fill counts and approved targets are identical.

## Identity of records (point-in-time IDs)

No identifier depends on the dataset as a whole (the WIP version hashed a full-dataset
fingerprint into every ID, so appending a bar renamed history — fixed):

```
account_id  = sha256(paper config fingerprint | strategy | strategy version | instrument | timeframe)[:12]
decision_id = <account_id>-D<YYYYMMDD of the decision session>
order_id    = <account_id>-O<YYYYMMDD of the scheduled execution session>
fill_id     = <account_id>-F<YYYYMMDD of the execution session>
snapshot_id = <account_id>-S<YYYYMMDD of the valued session>
event_id    = <account_id>-E<ledger sequence, 8 digits>
```

At most one decision, order and fill exist per account and session. The pending order at the
end of a replay has the same `order_id` as the order created when that session is processed.
Each decision stores `input_fingerprint`, a hash of its own `BarInput` only. A dataset
fingerprint is still computed for replay reports, as **metadata only**.

## Durable state and ledger (incremental mode)

One directory per account under the git-ignored `data/paper/accounts/<account_id>/`:

| File | Content |
|---|---|
| `manifest.json` | written once: app/paper version, git commit (+dirty), creation time, instrument, timeframe, strategy version, initial capital, paper/risk/backtest fingerprints, full configuration |
| `ledger.jsonl` | append-only events, one JSON object per line: `seq`, `event_id`, `event_type` (`order`, `fill`, `reconciliation`, `decision`, `snapshot`), `session`, `account_id`, `record`, `prev_hash`, `hash` (SHA-256 chain) |
| `state.json` | versioned (`schema_version` 2; schema 1 is read under explicit rules, below) account state: cash, quantity, cost basis, realized P&L, costs, full risk-manager state (HWM, locks, stop, lockout, `stop_activated`, current target), pending instruction (with its `exit_causes`), open round trip, last processed session, session counter, last decision, input fingerprint, config fingerprint, and the pointer (seq, hash) to the last committed ledger event |

**State schema 2 (ADR-0034 D-A9, implemented in the working tree, not committed).** Every
write is schema 2. A schema-1 state loads with `stop_activated = false` when that is
determined (the profile has no stop, or no position is open) and is **refused** when the
profile has a stop and a position is open (the stop episode is never inferred); the causes of
a pending schema-1 exit are derived only where the decision rules determine them, otherwise
the state is refused. Decision records gain `stop_activation` and `exit_causes`, trades gain
`exit_causes` (additive; older ledger records keep their fields and hashes). **Upgrade
precondition (not executed):** stop the scheduler, back up the account directories and make
sure no uncommitted ledger tail exists — a tail written by older code would not match the
reprocessed session. `drawdown_lock_20` and `atr_stop_3` accounts have a new `account_id`
(semantic versions) and are not reopened; the control account keeps its identity.

Choice: a local file pair instead of a database migration — the ledger is append-only and
per account, needs no queries, and keeps paper trading independent of the database schema.
State files are deterministic (no wall-clock time; floats in shortest round-trip form, so a
save/load cycle is bit-exact; NaN stored as `null`; strict JSON).

**Commit protocol** per session: process in memory → append the session's events to the
ledger (flush + fsync) → write `state.json` atomically (temp file, fsync, `os.replace`).
If the process dies after the ledger append but before the state write, the reopened store
keeps the extra events aside; re-processing the same session must reproduce them exactly
(processing is deterministic) and then completes the commit without appending — otherwise
`LedgerError`. A store object whose commit failed refuses further use until reopened.

**Fail safe, never repair.** Opening refuses (and changes nothing) when: the state file is not
valid JSON, has missing/unknown fields, a different schema version, invalid values (negative
cash or quantity, NaN account values, inconsistent open round trip), a different configuration
fingerprint or account identity; the ledger's hash chain is broken (an edited, deleted or
reordered event); the ledger is shorter than the committed pointer; or a ledger exists without
a state file.

**Idempotency.** A session ≤ the last processed session is skipped (`already_processed`): no
decision, order, fill, cash, position or P&L change; the state and ledger files are byte-for-
byte unchanged (tested for `process(T); process(T)` and `process(T); restart; process(T)`).
If the last processed session arrives again with a different input fingerprint (revised data),
`DataRevisionError` is raised instead of silently diverging. One writer per account directory.

## Temporal integrity (tests)

- Prefix vs full history: replay through T equals the first T sessions of a longer replay —
  every record, ID, equity row and ledger event.
- Future mutation: changing prices, volume, adjusted close, strategy states or indicators after
  T leaves everything through T unchanged, **including IDs and ledger hashes** (previously
  failing; fixed).
- Incremental ledger through T is unchanged when the bars after T differ.
- Decisions whose `effective_at` is not after their close are refused; a missing scheduled
  session rejects the order (`scheduled_session_missing_in_data`) and the next decision is
  filled normally; shuffled bars are refused.

## CLI

```bash
uv run python -m app.paper.cli replay --symbol SPY [--scenario control_no_overlay] [--start YYYY-MM-DD]
uv run python -m app.paper.cli run    --symbol SPY [--strategy sma_trend] [--scenario ...] [--start YYYY-MM-DD] [--until YYYY-MM-DD]
uv run python -m app.paper.cli status --symbol SPY [--strategy sma_trend] [--scenario ...]
```

`run` processes every completed bar in the local database that each account has not processed
yet (default: all six baselines); run it again after the next ingestion to process new bars.
`--start` sets the first result session of a **new** account; the decision at the close of the
session before it is recorded first (see *Window start*); an existing account ignores it.
`run` refuses (exit code 2, nothing created) to create an account when an account of the same
strategy, risk scenario, instrument and timeframe exists under another account id (an earlier
configuration, risk-mechanism semantic version or strategy version, ADR-0034); the existing
account is named, not opened and not changed. `status` is read-only: without an account it
reports `no_account` (and any account under another identity) and creates nothing. The
dashboard's "create account" (`paper_create_account`) applies the same identity check before
anything is written and also refuses when an account directory has a manifest that cannot be
read (its identity cannot be determined); the CLI ignores such directories, as before — a
deliberate difference. In both paths a new account next to a conflicting one requires an
explicit decision about the existing account first. `--until` limits processing to bars up to a date (simulating daily arrival). All
commands call `assert_safe_trading_mode` first.

## Results on SPY (8,479 sessions, 1993-01-29 → 2026-10-06)

Verification, not performance claims: with the control scenario, the six replays reproduce the
Phase 8 fills (same sessions and prices) and equity within 1e-6; with `volatility_target_10`
all four fill actions occur; incremental processing with restarts (including a restart before
every session of 2020-02-18 → 2020-04-30) reproduces the replay ledger exactly.

Runtime (local machine, median of 3, 8,479 sessions): inputs for the six baselines (strategies,
indicators, calendar) 5.2 s; replay of one strategy (net + gross shadow + ledger events) 5.3 s;
six-baseline replay including inputs 36.7 s; incremental processing with a durable commit
(ledger fsync + atomic state replace) per session 40.0 s for the full history (4.7 ms per
session; a daily run processes one session); reopening an account (load + verify 25,882 ledger
events) 0.5 s; a no-op re-run 0.02 s.

## Limitations

- Simulation only: fills at the exact raw open, fractional shares, no partial fills, no
  queue/market impact, no corporate-action handling beyond raw prices (no dividends credited),
  no cash interest, generic Phase 8 costs.
- Daily bars only; the next session must be present in the data to execute (a missing session
  rejects the order rather than guessing a price).
- Inputs (strategy states, indicators) are recomputed from the local database on each `run`;
  this relies on the causality of Phases 3–7 (tested there). Only the last processed session is
  checked for data revisions; earlier revisions are not detected by `run`.
- Incremental mode is single-writer per account (no locking).
- A new account starts flat (initial capital, no position or stop) at `start`; the history
  before it only rebuilds the requested target for the decision at the close of `start−1`
  (*Window start*). The reference snapshot of that decision is not marked per record; the
  dashboard labels it only as indicative (D1).
- The gross (zero-cost) curve is produced by replay only, not by incremental accounts.
- `status` recomputes strategy versions from the data (it needs the database).
