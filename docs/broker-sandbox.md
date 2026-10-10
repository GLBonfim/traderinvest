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

`target qty = approved exposure × sizing capital / close of the decision bar`;
`delta = target − sandbox position`; an exit sells exactly the sandbox position; never short.
The opening auction is the closest match to the project's next-session-open fill; the
fractional remainder is not traded. Limits: max order notional 12,000, max 4 sandbox orders per
day, allowed symbols {SPY}.

| Policy | Buys | Sells | Sizing capital |
|---|---|---|---|
| `opg_whole_shares_loo_buys` (**default**, ADR-0027) | **limit-on-open** (`limit`, `opg`), budget-bounded limit price | market-on-open | effective budget = 10,000 − 1.00 allowance = 9,999 |
| `opg_whole_shares` | market-on-open — blocked by the default capital policy | market-on-open | 10,000 |
| `day_fractional` (not default) | market `day`, fractional — blocked by the default capital policy | market `day` | 10,000 |

### Limit-on-open buys (ADR-0027, `app/broker/pricing.py`)

Alpaca (verified 2026-10-10): `opg` "with a market/limit order type" submits market-on-open
(MOO) / limit-on-open (LOO) orders for equities; `limit_price` is required for `limit`, with at
most 2 decimals at ≥ 1.00 (4 below, finer is rejected); limit orders take whole shares only.

`limit = floor_to_increment(effective budget / (held + bought))`, computed with `Decimal`, so
`(held + bought) × limit ≤ effective budget` exactly and the limit is the highest valid price
with that property (≥ the decision close, since the quantity was sized on that close). A buy
limit order never fills above its limit, so the resulting position's value at the fill price is
≤ 9,999, and with the 1.00 allowance ≤ 10,000 (Alpaca charges no commission on equity buys; the
allowance mirrors the project's flat research commission). The notional cap and the account
cash check use the limit price (worst case). The adapter refuses locally a limit order without
a price, with fractional shares or off the increment, and a market order with a price.

Example (pending SPY entry, decision close 778.570007 on 2026-10-09): 12 shares, limit 833.25,
worst-case cost 9,999.00. If the auction prints above 833.25 the order is not filled and Alpaca
cancels it after the open (`unfilled` in reconciliation; nothing is retried).

### Three separate limits (ADR-0026)

| Limit | What it bounds | Where |
|---|---|---|
| Sandbox **budget** `allocated_capital` (10,000) | cost of the resulting sandbox position (held + bought) | `capital_policy`, buys only |
| **Risk** limits `max_order_notional` (12,000), `max_orders_per_day` (4) | one order / one day | every order |
| Alpaca **account** | status `ACTIVE`, not blocked, estimated cost ≤ min(cash, buying power) — no margin | buys only, read before the POST |

A market order (MOO or `day`) has **no maximum fill price**: its cost is quantity × the auction
price, unknown at submission. The budget therefore cannot be guaranteed for a market buy, and
the default `capital_policy = block_unbounded` **blocks every market buy**
(`capital_bound_unenforceable`). Sells (exits, reductions) are never held back by capital
checks. `reference_price_unguaranteed` checks the budget at the decision close only — it is not
a guarantee, is never the default, and exists for tests of the submission mechanics and as an
explicit owner choice. Reconciliation reports any buy whose actual filled cost exceeded the
budget (`budget_breaches`, alert `BROKER_RECONCILIATION_MISMATCH`). The owner chose the
guaranteed alternative — limit-on-open buys (ADR-0027) — which is now the default order policy;
a limit order is price-bounded, so the capital policy lets it through.

### OPG submission window (ADR-0026, `app/broker/timing.py`)

Alpaca: "OPG orders submitted after 9:28am but before 7:00pm ET will be rejected"; after 7:00pm
they queue for the following day's opening auction; unfilled OPG orders are cancelled after the
open. An `opg` order for the auction of session S is sent only if, in America/New_York:

- now ≥ 19:00 of the XNYS session before S (+ `opg_safety_seconds`, default 60 s), and
- now < 09:28 of S (− 60 s), and
- now is not inside the daily 09:28–19:00 band (− / + 60 s). The documentation does not say
  whether the band applies on weekends and holidays, so it is applied every day (conservative).

Sessions come from the XNYS calendar (weekends, holidays, early closes; "previous session" is
never simply yesterday); zoneinfo handles DST. The window is checked with the other gates **and
again immediately before the POST**. Reasons: `opg_window_not_open`, `opg_cutoff_passed`,
`opg_rejection_window`, `opg_schedule_invalid`. `day` orders are not subject to it.

## Gates (every one must pass; a failing gate blocks and is recorded, nothing raises)

1. kill switch released; 2. armed (default **unarmed**; `arm --confirm-sandbox-only`);
3. `TRADING_MODE=paper` (the default is `disabled`); 4. sandbox credentials present
(`ALPACA_PAPER_API_KEY_ID`, `ALPACA_PAPER_API_SECRET_KEY`, environment only); 5. symbol allowed;
6. OPG submission window; 7. not already submitted; 8. no short; 9. notional cap; 10. sandbox
budget / capital policy (buys); 11. sandbox account tradable with enough cash (buys); 12. daily
order cap; 13. instruction not stale (its session open is still in the future); 14. OPG window
re-checked at the POST. Nothing is linked by default.

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
- `opg` orders are sent only inside the OPG window above; `sync` is manual (the scheduler is not
  connected to the broker in this phase).
- With the default capital policy no market buy is sent at all (ADR-0026); entries are
  limit-on-open (ADR-0027) and are not filled when the auction prints above the limit — the
  sandbox then stays flat while the local simulation (which always fills at the open) is long,
  which reconciliation reports as a mismatch plus an `unfilled` order.
- Alpaca's order-type table marks OPG (MOO and LOO) with "Please contact the sales team for any
  TIF marked with a*" and does not say whether this applies to Trading API / paper accounts. It
  is unverified until the first sandbox submission; a rejection is recorded and never retried.
- Alpaca does not document the reference price of its buying-power check for OPG orders
  submitted outside regular hours.
- A fractional sandbox holding (e.g. from a manual trade) makes the whole-share delta
  fractional, which Alpaca rejects for `opg`; nothing in the project creates such a holding.
- Paper fills are simulated by the vendor (no dividends, random partial fills).
