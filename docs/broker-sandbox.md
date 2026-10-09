# Broker Sandbox Integration (Phase 15)

> **BROKER SANDBOX — Alpaca paper account, no real money.** The only endpoint this project uses
> is `https://paper-api.alpaca.markets`. `https://api.alpaca.markets` is explicitly rejected, and
> no parameter, setting or environment variable can select another endpoint. `TradingMode`
> stays {disabled, paper}; `refuse_real_money_order()` is unchanged. **No external sandbox
> order has been submitted**; the first one requires explicit owner approval.

Code: `app/broker/` (`config.py`, `gateway.py`, `alpaca.py`, `store.py`, `executor.py`,
`reconcile.py`, `cli.py`) · dashboard page "Broker Sandbox" · ADR-0025.

## Architecture

```
linked local paper account (Phase 12 ledger: RiskManager-approved decisions)
      ↓ derive_intent (latest decision; pending entry/exit/rebalance; scheduled open in future)
SandboxExecutor (gates, sizing, idempotency, write-ahead log)
      ↓ BrokerGateway (project abstraction; no vendor object crosses it)
AlpacaPaperGateway (official REST API, stdlib HTTP, fixed sandbox URL, no redirects)
```

The local `PaperBroker` remains the project's execution simulation and is unchanged. The sandbox
**mirrors** its risk-approved instructions into an external paper account; it never decides
anything. There is no API, CLI command or dashboard control to place an arbitrary order: the
executor's only public method is `sync()`, which derives orders from paper decisions (tested by
inspection). No cancel/replace surface exists.

## Verified vendor facts (Alpaca documentation, 2026-10-08)

- Paper base URL `https://paper-api.alpaca.markets`; paper API keys are separate from other keys.
- Authentication headers `APCA-API-KEY-ID`, `APCA-API-SECRET-KEY`.
- `POST /v2/orders`: `client_order_id` ≤ 128 characters; `time_in_force` `opg` executes only in
  the opening auction; fractional and notional orders are accepted only with `day`.
- `GET /v2/orders:by_client_order_id`, `GET /v2/account`, `GET /v2/positions/{symbol}`.
- Paper limitations: no market impact or latency slippage, no dividends, random partial fills.

## Sizing and order policy

`target qty = approved exposure × allocated capital (10,000) / close of the decision bar`;
`delta = target − sandbox position`; an exit sells exactly the sandbox position; never short.
Default policy `opg_whole_shares` (market, `opg`, whole shares — the opening auction is the
closest match to the project's next-session-open fill); the fractional remainder is not traded.
`day_fractional` exists but is not the default (decision for the owner). Limits: max order
notional 12,000, max 4 sandbox orders per day, allowed symbols {SPY}.

## Gates (every one must pass; a failing gate blocks and is recorded, nothing raises)

1. kill switch released; 2. armed (default **unarmed**; `arm --confirm-sandbox-only`);
3. `TRADING_MODE=paper` (the default is `disabled`); 4. sandbox credentials present
(`ALPACA_PAPER_API_KEY_ID`, `ALPACA_PAPER_API_SECRET_KEY`, environment only); 5. symbol allowed;
6. not already submitted; 7. no short; 8. notional cap; 9. daily order cap; 10. instruction not
stale (its session open is still in the future). Nothing is linked by default.

## Idempotency and restart recovery

- Deterministic `client_order_id = qp-<paper account>-D<decision session>` (point-in-time).
- Write-ahead: `intent` and `submit_attempted` are appended to `data/broker/orders.jsonl`
  (hash-chained) **before** the request.
- Before every POST the order is looked up by `client_order_id`; an existing order is adopted.
- An ambiguous network failure records `unknown`; the next sync resolves it by lookup.
- On restart, open/attempted orders are refreshed by `client_order_id`; an attempt confirmed
  absent at the broker may be retried with the same id. Tested: crash before and after the POST
  → exactly one order.

## Reconciliation

Expected sandbox quantity = the executor's sizing of the decision behind the local paper
account's last fill (0 when the local simulation is flat); a difference of ≥ 1 share (whole-share
policy) is a mismatch (`BROKER_RECONCILIATION_MISMATCH`). Partial fills are listed. Nothing is
corrected automatically.

## Kill switch

`broker kill` (CLI) or the dashboard button engages it immediately (alert
`BROKER_KILL_SWITCH_ENGAGED`); release is CLI-only (`release-kill --confirm`).

## Alerts

`BROKER_ORDER_SUBMITTED` (INFO), `BROKER_ORDER_BLOCKED`, `BROKER_ORDER_FAILED`,
`BROKER_RECONCILIATION_MISMATCH`, `BROKER_KILL_SWITCH_ENGAGED` (WARNING); every message says
"BROKER SANDBOX (paper account, no real money)". Blocks caused by the default unarmed state are
not alerted (they are the normal state).

## CLI

```bash
uv run python -m app.broker.cli status
uv run python -m app.broker.cli link <paper_account_id>      # unlink <id>
uv run python -m app.broker.cli sync                          # DRY RUN (default)
uv run python -m app.broker.cli reconcile                     # read-only
uv run python -m app.broker.cli kill [--reason ...]          # release-kill --confirm
uv run python -m app.broker.cli arm --confirm-sandbox-only   # disarm
uv run python -m app.broker.cli verify
```

`sync --submit` is the only command that can send a sandbox order. It has not been run.

## Security

- Credentials: environment only; never in repr, logs, error messages, the order log or the
  dashboard (which shows only whether they are present); `.env.example` lists the variable names
  without values.
- Redirects are never followed (a redirect could forward the key headers elsewhere).
- `app/broker/alpaca.py` is the only module with HTTP code; no vendor SDK is installed.

## Limitations

- One vendor, one symbol; whole-share opening-auction orders (fractional remainder not traded).
- The sandbox mirror sizes against a fixed allocated capital, not the sandbox account equity.
- `opg` orders must reach the broker before the opening auction cut-off; `sync` is manual
  (the scheduler is not connected to the broker in this phase).
- Paper fills are simulated by the vendor (no dividends, random partial fills).
