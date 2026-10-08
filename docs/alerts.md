# Alerts & Monitoring (Phase 14)

> Alerts **report** events that the engines, the risk layer and the paper simulation already
> recorded. They are downstream consumers only: an alert never creates a decision, an
> instruction or an order, and it is never a recommendation. Paper-related alerts are
> **PAPER SIMULATION** events; nothing is sent to any brokerage or market.

```
existing domain event  ->  alert rule  ->  alert event  ->  delivery channel
```

Code: `app/alerts/` (`models.py`, `rules.py`, `store.py`, `channels.py`, `monitor.py`,
`engine.py`, `sources.py`, `cli.py`) · dashboard page "Alerts & Monitoring" · ADR-0023.

## Event types

Severity describes operational attention only — never a financial opportunity.

| Type | Severity | Source | Rule (explicit) |
|---|---|---|---|
| `DATA_STALE` | WARNING | MarketData | completed XNYS sessions after the last stored bar (one alert per episode, keyed by the last stored session) |
| `DATA_MISSING_SESSION` | WARNING | MarketData | an XNYS session inside the stored history has no stored bar |
| `DATA_INGESTION_FAILED` | WARNING | MarketData | an `ingestion_runs` row whose status is not `succeeded` |
| `DATA_PROVIDER_FAILURE` | CRITICAL | MarketData | a stored data-quality event `provider_failure` |
| `DATA_QUALITY_EVENT` | mapped from the stored event (info/warning/critical) | MarketData | any other stored data-quality event |
| `REGIME_TREND_CHANGED`, `REGIME_VOLATILITY_CHANGED`, `REGIME_MOMENTUM_CHANGED`, `REGIME_COMPOSITE_CHANGED` | INFO | MarketRegimeEngine | `label(T) != label(T-1)` |
| `REGIME_VOLATILITY_EXTREME` | WARNING | MarketRegimeEngine | volatility label becomes `extreme` |
| `STRATEGY_STATE_CHANGED` | INFO | StrategyEngine | (previous, current) ∈ {INSUFFICIENT_DATA→LONG, INSUFFICIENT_DATA→FLAT, FLAT→LONG, LONG→FLAT} |
| `RISK_EXPOSURE_REDUCED` | WARNING | RiskManager | 0 < approved < requested, on entering this category |
| `RISK_EXPOSURE_BLOCKED` | WARNING | RiskManager | requested > 0 and approved = 0, on entering this category |
| `RISK_DRAWDOWN_WARNING` | WARNING | RiskManager | risk state NORMAL → WARNING |
| `RISK_LOCK_COOLDOWN_STARTED` | WARNING | RiskManager | risk state → LOCKED (drawdown or session lock; cooldown starts with the lock) |
| `RISK_LOCK_COOLDOWN_ENDED` | INFO | RiskManager | risk state LOCKED → other |
| `RISK_STOP_TRIGGERED` | WARNING | RiskManager | `stop_triggered` in the decision reasons (close-based stop) |
| `PAPER_ENTRY_SCHEDULED`, `PAPER_EXIT_SCHEDULED`, `PAPER_REBALANCE_SCHEDULED` | INFO | PaperTrading | a decision's risk-approved instruction has `pending_kind` entry / exit / rebalance (the "instruction created" alerts; instructions that need no order are not reported) |
| `PAPER_FILL_EXECUTED` | INFO | PaperTrading | a simulated fill in the ledger |
| `PAPER_ORDER_REJECTED` | WARNING | PaperTrading | a simulated order rejected by the paper broker's safety checks |
| `PAPER_ACCOUNT_ERROR` | CRITICAL | PaperTrading | manifest unreadable or not a pre-declared scenario |
| `PAPER_STATE_CORRUPT` | CRITICAL | PaperTrading | `state.json` fails validation |
| `PAPER_LEDGER_VERIFICATION_FAILED` | CRITICAL | PaperTrading | ledger hash chain broken or inconsistent with the committed pointer |
| `SYSTEM_DATABASE_UNAVAILABLE` | CRITICAL | System | PostgreSQL unreachable |
| `SYSTEM_DASHBOARD_RESULTS_STALE` | INFO | System | persisted dashboard validation/ML results missing for the current dataset |
| `SYSTEM_UNSUPPORTED_TRADING_MODE` | CRITICAL | Safety | trading mode not in {disabled, paper} |
| `SAFETY_NEGATIVE_CASH`, `SAFETY_NEGATIVE_POSITION`, `SAFETY_EXPOSURE_ABOVE_LIMIT`, `SAFETY_ACCOUNTING_RECONCILIATION_FAILED` | CRITICAL | Safety | paper snapshot invariants: cash ≥ 0, quantity ≥ 0, exposure ≤ 1, \|total P&L − (equity − initial)\| ≤ 1e-6 × initial |
| `OPS_STAGE_FAILED`, `OPS_SESSION_DATA_MISSING`, `OPS_LOCK_CONFLICT` | WARNING | Operations | a pipeline stage failed for a session (key: stage, session, account); the expected bar is still missing after the bounded retries; another instance holds the pipeline lock (Phase 14.5, docs/operations.md) |
| `OPS_CATCH_UP_REQUIRED` | INFO | Operations | more pending sessions than one run processes |
| `OPS_REPEATED_FAILURE` | CRITICAL | Operations | the configured number of consecutive failed runs (once per streak) |
| `TEST_NOTIFICATION` | INFO | System | only from `alerts test-channel` (never a market/trading event) |

Every message is a fixed template filled with recorded values; the structured reason is in
`payload` (previous/current labels, measures, requested/approved exposure, reasons, fill
values...). No language model is involved anywhere.

## Transitions, not states (no alert storms)

Rules fire when a state **changes**: a regime that stays the same, a strategy that stays LONG,
a lock that remains active, a risk reduction that persists over consecutive sessions — none of
these repeat. Wall-clock conditions (database down, unsupported mode, corrupt account,
dashboard results missing) use **episodes**: one alert when the condition starts, none while it
persists, re-armed once it clears. Stale data is one alert per last-stored session.

## Identity, deduplication, point-in-time

```
dedup_key = event_type | instrument | strategy or account | session (or episode/record id) | state
alert_id  = sha256(dedup_key)[:16]
```

Only information available when the underlying event happened enters the key — never a
dataset fingerprint, the application version or the time of the run. The store keeps the first
record of each `alert_id`; re-processing a bar, refreshing the dashboard or restarting stores
nothing new (counted as deduplicated). Rules for session T read rows at or before T only.
Tested: alerts through T from a history ending at T equal those from the full history; mutating
prices/volume after T does not change any alert through T; appending bars only adds alerts.

## Reporting window

A new store starts reporting at `since` (CLI `--since`; default: the latest stored session), so
the first run does not replay decades of history. State machines (e.g. risk categories, risk
state) still run over the full ledger/history; only reporting is limited.

## Persistence (git-ignored `data/alerts/`)

| File | Content |
|---|---|
| `alerts.jsonl` | append-only, SHA-256 hash-chained alert records + `recorded_at` |
| `deliveries.jsonl` | append-only, hash-chained delivery attempts (alert, channel, attempt, ok, error, permanent failure) |
| `state.json` | schema version, `since`, active monitor conditions (episodes), channel enablement times, counters (evaluations, deduplicated); atomic replace |

A broken chain or invalid state raises `AlertStoreError`; nothing is repaired. No database
migration: alerts are append-only, local and never queried relationally (ADR-0023).

## Delivery channels

| Channel | Enabled | Notes |
|---|---|---|
| console | always | one structured log line per alert (no secrets) |
| dashboard | always | the Alerts & Monitoring page reads the store |
| webhook | only if `ALERT_WEBHOOK_URL` is set | generic JSON POST (`{"kind": "quant-platform-alert", "alert": …}`), stdlib HTTP, 5 s timeout, `ALERT_WEBHOOK_MIN_SEVERITY` (default WARNING) |

No vendor SDK (email/Telegram/Discord) was added; a generic webhook keeps coupling minimal.
The webhook URL may contain a token: it is read from the environment, never logged, stored or
shown (errors are stored with URLs redacted).

**Retry policy** (bounded, non-blocking): at most 3 attempts per (alert, channel); attempt 2 is
made on a later run ≥ 60 s after attempt 1, attempt 3 ≥ 300 s after attempt 2; then the delivery
is marked as a permanent failure. Nothing sleeps. A channel enabled later does not replay alerts
recorded before it was first seen. Delivery is at-least-once: a crash between sending and
recording can repeat one notification (the payload carries `alert_id` for receiver-side
de-duplication).

**Isolation**: delivery reads stored alerts and appends delivery records only. Failures are
recorded, never raised, and cannot affect research or paper-trading state (tested: paper files
and alert records are byte-identical after failed deliveries). `app/alerts` imports no trading
module (paper broker/store/trader, risk manager) and no network library except the stdlib HTTP
client in `channels.py` (AST tests).

## Monitoring

`run_alerts` and the dashboard compute a lightweight health state: database, market-data
freshness (XNYS calendar), latest ingestion run, paper accounts (state + ledger verification,
read-only — `PaperStore` is not used, so no commit is ever completed by monitoring), alert-store
integrity, safety (trading mode), application version. Operational counters: alerts stored,
deduplicated, evaluations, deliveries succeeded / failed / failed permanently, by severity, by
source — not performance metrics. No Prometheus/Grafana.

## CLI

```bash
uv run python -m app.alerts.cli run [--since YYYY-MM-DD] [--no-deliver]
uv run python -m app.alerts.cli list [--severity WARNING] [--source RiskManager] [--limit 20]
uv run python -m app.alerts.cli status
uv run python -m app.alerts.cli verify
uv run python -m app.alerts.cli test-channel [--channel console|webhook]
```

Typical daily use: ingest → `paper.cli run` → `alerts.cli run`.

## Dashboard

"Alerts & Monitoring" page: monitoring table, operational counters, filters (severity — default
CRITICAL + WARNING —, source, strategy, account, date range), alert table with delivery status,
and an alert detail (message, identity fields, structured payload). An explicit button runs an
evaluation (writes to the alert store only).

## Limitations

- Evaluation is pull-based (CLI or dashboard button); there is no scheduler/daemon.
- Invocations of `refuse_real_money_order()` are not observable without changing the safety
  module (ADR-0006 requires owner approval); unsupported trading modes are monitored instead.
- Paper-account alerts cover accounts under `data/paper/accounts/`; research risk overlays
  without an account are not monitored.
- `LONG → INSUFFICIENT_DATA` and participation-regime changes are not alert types.
- Wall-clock condition alerts (database, mode, corruption) are detected only when a run happens.
- At-least-once external delivery (see above); single writer per alert store.
