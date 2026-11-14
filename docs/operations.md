# Daily Operations & Scheduler (Phase 14.5)

> **PAPER SIMULATION / LOCAL OPERATIONS.** The operations layer runs the existing system in the
> right order, once per completed XNYS session, entirely locally. It adds no trading capability:
> no broker, no real money, no order routing. Paper trading remains a local simulation.

Code: `app/operations/` (`config.py`, `sessions.py`, `models.py`, `lock.py`, `store.py`,
`pipeline.py`, `services.py`, `scheduler.py`, `cli.py`) · dashboard page "Operations" · ADR-0024.

## Pipeline

```
PRECHECK
  └─ for each target session, oldest first:
       INGEST  -> VALIDATE -> PAPER -> ALERTS
HEALTH
COMPLETE
```

| Stage | Delegates to | Idempotency |
|---|---|---|
| PRECHECK | trading mode, database, schema at head, instrument spec, XNYS calendar, storage access, paper-account readability (non-critical) | read-only |
| INGEST | `app.data.ingestion.ingest_daily_bars` for exactly that session (yfinance provider — the only network access) | skipped when the closed bar is already stored; ingestion itself is idempotent |
| VALIDATE | stored closed bar: present, unique, prices > 0, consistent OHLC, volume ≥ 0, previous calendar session stored | read-only |
| PAPER | every account in `data/paper/accounts/`: Phase 12 `PaperStore.process_many` with inputs built from bars ≤ the session (point-in-time) — all financial state via RiskManager | the store skips processed sessions |
| ALERTS | `app.alerts.engine.run_alerts` (market freshness evaluated as of the session) | alert ids deduplicate |
| HEALTH | database, schema, freshness, latest ingestion, paper ledgers, alert store, safety, version, checkpoint | read-only |
| COMPLETE | run record | — |

The orchestration contains no indicator, strategy, risk, accounting or alert-rule logic
(`pipeline.py` only decides what runs and in which order; `services.py` is a thin adapter).
Each stage records start, end, status (`ok`, `skipped`, `warning`, `failed`), a structured
result and the error (type + message, no secrets).

## Target sessions (XNYS-aware)

- A session is **eligible** once its official regular-session close **+ grace** (default
  30 minutes, a transparent operational convention so the provider can publish the daily bar)
  has passed. Closes come from the XNYS calendar, so early closes (e.g. 13:00 ET), holidays,
  weekends and DST are handled; the machine's time zone is never used (all times UTC).
- An incomplete current session is never processed.
- Targets = calendar sessions in (checkpoint, latest eligible]. The checkpoint is the last
  session completed by operations; on the very first run it is the session before the latest
  stored bar, so that session gets a verification pass (ingestion skipped).
- No target → `NO_NEW_COMPLETED_SESSION` (no history record, no side effect; counted).
- **Catch-up** after downtime: missing sessions are processed one by one, oldest first —
  ingest/verify → paper → alerts for each — never jumping to the latest. At most
  `max_catch_up_sessions` (10) per run; the rest follow in the next run
  (`OPS_CATCH_UP_REQUIRED`).

## Failure policy

| Failure | Behaviour |
|---|---|
| precheck (critical check) | `PRECHECK_FAILED`; nothing else runs |
| INGEST (provider/network) | session `FAILED` (ingestion failure, recorded in `ingestion_runs`); later sessions not attempted; paper never processes it |
| VALIDATE: bar missing (late data) | `WAITING_FOR_DATA`; bounded retries: up to 3 attempts, ≥ 30 min apart for scheduled runs; then `DATA_MISSING` + `OPS_SESSION_DATA_MISSING`; scheduled runs retry only once a newer session is eligible; manual runs always retry. Never fabricated |
| VALIDATE: bar invalid | session `FAILED` |
| PAPER: stage error (e.g. bars unreadable) | session `FAILED`; checkpoint not advanced |
| PAPER: one account fails | **isolated**: other accounts are processed; the session completes with warnings; `OPS_STAGE_FAILED` names the account; the account catches up in order through its own store on a later session |
| ALERTS (including delivery) | warning only; nothing is rolled back (delivery has its own bounded retries) |
| HEALTH | warning only |
| `consecutive failed runs = 3` | `OPS_REPEATED_FAILURE` (once per streak) |

Run statuses: `COMPLETED`, `COMPLETED_WITH_WARNINGS`, `WAITING_FOR_DATA`, `FAILED`,
`PRECHECK_FAILED`, `NO_NEW_COMPLETED_SESSION`, `NOT_DUE`, `LOCKED`, `DRY_RUN`.

## Idempotency and crash recovery

The durable truth lives in the subsystems — stored bars, paper ledgers (Phase 12), the alert
store (Phase 14) — all idempotent. The operations checkpoint only narrows what is pending and is
advanced after a session's ALERTS stage. After a crash anywhere (before/during ingestion, after
ingestion, during/after paper, during alert delivery, before recording completion) the next run
re-runs the unfinished session: ingestion is skipped because the bar is stored, paper skips
processed sessions, alerts deduplicate. Tested with injected crashes on synthetic services and
on SPY: no duplicate bars, decisions, fills, P&L or alerts; the interrupted run is shown as
`INTERRUPTED` in the history.

## Locking

`data/operations/pipeline.lock` is created atomically (`O_CREAT | O_EXCL`) with the owner's
lock id, host, PID, acquisition time and purpose, and removed on exit only if it is still ours.
A second instance gets `LOCKED` (+ `OPS_LOCK_CONFLICT`) and does nothing. A lock is broken
automatically **only** if it was taken on this host and its process is verified dead (Windows:
`OpenProcess`/`GetExitCodeProcess`; `os.kill(pid, 0)` is not used on Windows because it would
terminate the process). Locks of live processes (possibly reused PIDs) or other hosts are never
removed automatically: `ops unlock --force` is the explicit operator action.

The short check-then-act sections run under an OS mutex on the persistent
`pipeline.lock.mutex` (ADR-0032). If that mutex cannot be obtained within its timeout the lock
state is unknown: `LockMutexTimeoutError` (a `LockHeldError`, so the run is still `LOCKED` and
does nothing) and the alert says the lock *could not be checked* — it is not reported as proof
that another pipeline holds it. `unlock --force` in the same situation raises `LockReleaseError`
and leaves the lock unchanged.

## Persistence (`data/operations/`, git-ignored)

| File | Content |
|---|---|
| `runs.jsonl` | append-only, SHA-256 hash-chained events: `run_started`, `stage`, `session_finished`, `run_finished` — written as they happen |
| `state.json` | versioned checkpoint (atomic replace, read-modify-write): last completed session, late-data attempts, consecutive failures, scheduler heartbeat, last run, counters |
| `pipeline.lock` | the lock above |

Corrupt history raises `OpsStoreError` (nothing repaired). Run ids:
`ops-<first target>-<last target>-<config fingerprint>-<UTC timestamp>` — no dataset
fingerprint, so appending bars never renames past runs.

## Scheduler

A single long-running local process (`ops scheduler`), started by the user (ADR-0024). Every
`scheduler_poll_seconds` (300) it records a heartbeat and runs the pipeline in `scheduled` mode
only when a run is due: a new session became eligible, or a late-data retry interval elapsed.
Extra wake-ups are harmless: the checkpoint, the lock and the subsystems' idempotency turn them
into no-ops, so the polling frequency never changes any financial behaviour. Time and sleep are
injected; tests never wait. To start it at logon, the user may create a Windows Task Scheduler
entry for `uv run python -m app.operations.cli scheduler` — no OS scheduler is installed or
configured by the project. No Celery, Redis, APScheduler or services.

Failure policy (ADR-0033). Stage failures are already results inside the pipeline; an exception
that escapes a scheduled run is caught (`Exception` only — `KeyboardInterrupt`/`SystemExit` are
never absorbed), logged once (`operations.scheduled_run_failed`, single-line message with URL
credentials masked) and written to the heartbeat (`last_run_status = RUN_ERROR`, `last_error`,
`consecutive_failures`, `retry_not_before`). The pipeline's lock is released by the pipeline
itself (`finally`). The scheduler then keeps polling but does not retry before
`poll × 2^(n−1)` seconds (capped at 1 h); after 3 consecutive failed runs, or immediately after a
`LockReleaseError` or `OpsStoreError` (lock or store state uncertain), it **halts**: the heartbeat
shows `halted`, `ops status` reports "halted: …", and an operator must investigate and restart
it. Ctrl+C stops the loop (logged as `operations.scheduler_stopped`).

## CLI

```bash
uv run python -m app.operations.cli run [--dry-run]   # process pending completed sessions
uv run python -m app.operations.cli status            # checkpoint, last run, lock, scheduler, metrics
uv run python -m app.operations.cli history [--limit 10]
uv run python -m app.operations.cli verify            # history integrity
uv run python -m app.operations.cli next              # latest eligible / next eligibility / pending
uv run python -m app.operations.cli scheduler         # long-running local scheduler (Ctrl+C)
uv run python -m app.operations.cli unlock --force    # operator: remove a verified-dead lock
```

`--dry-run` shows target sessions, intended stages, whether ingestion would occur, configured
paper accounts, that alerts would run and the lock owner — and writes nothing (no lock, no
history, no state, no bars, no paper or alert changes; tested byte-for-byte).

## Dashboard

"Operations" page (13th): latest completed / next eligible / latest ingested / latest paper /
checkpoint / scheduler status, latest alert evaluation, lock, last run with stage durations and
errors, recent runs, operational metrics; controls **Dry run** and **PAPER SIMULATION / LOCAL
OPERATIONS: run pending sessions now**. Scheduler status comes from its heartbeat (no
start/stop controls, no process management).

## Operational alerts (Phase 14 store, source `Operations`)

`OPS_STAGE_FAILED` (WARNING), `OPS_SESSION_DATA_MISSING` (WARNING), `OPS_CATCH_UP_REQUIRED`
(INFO), `OPS_LOCK_CONFLICT` (WARNING), `OPS_REPEATED_FAILURE` (CRITICAL). Ingestion failures and
stale data are additionally reported by the existing `DATA_INGESTION_FAILED` / `DATA_STALE`.

## Observability and metrics

Structured logs (pipeline/scheduler events with run id, status, targets); no secret, `.env`
value, password or webhook URL is logged or stored. Operational metrics (`ops status`): runs,
no-op runs, not-due runs, lock conflicts, sessions processed, failed runs, data retries,
consecutive failures, interrupted runs, mean/max seconds per stage — not trading performance.

## Safety

`TradingMode` remains {disabled, paper}; the CLI checks it first; the pipeline prechecks it.
`app/operations` imports no HTTP library, broker SDK, scheduler framework or trading internals
(AST test); its only network path is the existing data provider during INGEST (and the existing
optional alert webhook).

## Recovery procedures (manual; preserve evidence first)

Nothing below is automated. No tool deletes, rebuilds, re-chains or rewrites a hash-chained
history; doing so would destroy the evidence the chain exists to provide. Every step is an
explicit operator decision.

### Research run registry: corrupt `index.jsonl` (B3)

The registry (`data/research_runs/`, ADR-0030) refuses a corrupt history: readers raise
`RegistryCorruptError`, writers refuse to append.

1. **Recognise the problem.**
   `uv run python -m app.validation.cli verify --registry data/research_runs` exits `3` with
   "registry history is corrupt: … line N …" → the **index** is damaged (invalid JSON, broken
   hash chain, impossible event sequence). Exit `1` with per-run problems (`artifact missing`,
   `artifact altered`, `artifact not a regular file`, `manifest does not match …`,
   `published directory present although the run failed`) → the **index is intact** and
   individual **run directories** are damaged or incomplete; those runs are never shown as
   complete and nothing else needs repair.
2. **Stop writers and preserve evidence before anything else.** Make sure no `record` is
   running and no `pipeline.lock` exists in the registry root. Copy the whole registry
   directory, unchanged, to a dated location outside the repository (e.g.
   `C:\dev\backup\research_runs-<UTC timestamp>`) and record the SHA-256 of every copied file.
   Work only on copies from then on.
3. **Diagnose.** Note the first failing line reported by `verify`. A damaged **last** line
   (truncated JSON) is typical of a crash or power loss during an append; a damaged line in
   the middle, or a valid line whose hash no longer matches, means the file was modified.
   Compare with earlier backups if any exist.
4. **Decide explicitly (and write the decision down).** Options, in order of preference:
   - keep the corrupt registry frozen as evidence and point future runs at a **new, empty
     registry directory**; published run directories in the old root remain readable files
     but are no longer verified and must not be presented as verified results;
   - restore an earlier **verified** backup of the whole registry; runs recorded after that
     backup then appear as `unregistered` directories and must not be treated as complete.
   Events that are not available in any copy cannot be recovered; do not reconstruct them.
5. **Verify afterwards** with `verify` (exit `0` required for the chosen registry) and compare
   the SHA-256 of the remaining artifacts with their manifests.

The same principles apply to the other hash-chained stores (`data/operations/runs.jsonl`,
alert and paper ledgers): they raise on corruption and are never repaired automatically.

### After merging a change to the lock implementation (B4)

The OS mutex of ADR-0032 only protects processes that run the new code: a process started
before the change keeps the old lock code until it is restarted, and old and new code must
never contend for the same data directory.

Long-running processes that use `PipelineLock`:
- the scheduler (`app.operations.cli scheduler`);
- the Streamlit dashboard (its Operations page reads the lock and can run the pipeline).

The FastAPI/Uvicorn app does **not** import `app.operations` (import-boundary tests), so it
does not need a restart for the lock. Short-lived CLI commands (`ops run`, `record`) simply must
not be running at the moment of the switch.

1. Before: list the running Python processes and their command lines
   (`Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select ProcessId, CommandLine`);
   check `ops status` (no `pipeline.lock` owner, scheduler heartbeat) and record `git rev-parse
   HEAD`.
2. Stop the scheduler and the dashboard in a controlled way (Ctrl+C) while no run is in
   progress (no lock owner).
3. Switch the code; record the new `git rev-parse HEAD`.
4. Restart them; confirm the new process ids, a fresh heartbeat (`started_at`) and that
   `ops status` / `ops verify` are clean.

## Limitations

- One instrument (SPY) and one provider; daily bars only.
- The scheduler must be started by the user and runs while the machine is on; downtime is
  recovered by catch-up.
- Precheck "calendar" uses the machine clock to check the calendar's horizon.
- One pipeline at a time per data directory (by design); no distributed locking.
- Shared read-only helpers are imported from `app.dashboard.services` (dataset identity,
  freshness, health). The hash-chain and atomic-write primitives live in `app.core.integrity`
  (the operations store still imports them through their `app.alerts.store` aliases).
