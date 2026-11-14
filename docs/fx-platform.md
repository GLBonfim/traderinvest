# TraderInvest FX — desktop Forex platform

A native Windows application (PySide6) that runs five trading agents, a central orchestrator
and a central risk manager against a **simulated** Forex market, shown as an isometric
pixel-art operations room. Decisions: ADR-0036 (scope, environments, agents), ADR-0037
(desktop and engine protocol), ADR-0038 (trading day and daily risk).

> **No real money.** The REAL environment is blocked in code (ADR-0006). In SIMULATION every
> price is synthetic and labelled as such. Simulated results say nothing about real
> performance, and agent scores are heuristic confidences, not probabilities.

## Architecture

```
 ┌──────────────── TraderInvestFX.exe (UI process, PySide6) ───────────────┐
 │  Operations room (QPainter, procedural pixel art) · panels · dialogs     │
 │  EngineSupervisor (QProcess)  ──starts──►  EngineClient (QTcpSocket)     │
 └───────────────────────────────────────────────┬──────────────────────────┘
                     JSON lines on 127.0.0.1, session token, protocol v1
 ┌───────────────── TraderInvestFX.exe --engine (engine process) ──────────┐
 │  EngineService (asyncio): tick loop · snapshots · events · commands      │
 │  Orchestrator                                                            │
 │    agents: Trend · Momentum · Mean Reversion · Regime filter             │
 │    → Portfolio Coordinator → RiskManager → execution port               │
 │  SIMULATION adapters: SyntheticMarket (bid/ask, sessions, rollover)      │
 │                       SimBroker (netting, margin, SL/TP, faults)        │
 │  FxStore (SQLite): hash-chained journal + atomic state documents         │
 └──────────────────────────────────────────────────────────────────────────┘
```

| Package | Contents |
|---|---|
| `app/fx/models.py` | pairs, quotes (bid/ask, origin), bars |
| `app/fx/environment.py` | SIMULATION / DEMO / REAL (REAL refused) |
| `app/fx/calendar.py` | 17:00 New York trading day, 24/5, holidays, sessions |
| `app/fx/proposals.py`, `app/fx/agents/` | agent proposals and the five agents |
| `app/fx/risk.py` | daily entries, locks, kill switch, sizing, entry checks |
| `app/fx/accounting.py` | multi-currency netting book, margin, exposure |
| `app/fx/lifecycle.py`, `app/fx/orders.py` | order states, requests, fills, positions |
| `app/fx/orchestrator.py` | the decision loop, execution, recovery, UI snapshot |
| `app/fx/sim/` | synthetic market and simulated broker |
| `app/fx/store.py`, `app/fx/config.py` | persistence and configuration |
| `app/engine/` | engine process, protocol, single-instance lock |
| `app/desktop/` | main window, operations room, panels, dialogs, icon, theme |
| `packaging/` | PyInstaller spec, build script, notices, draft licence |

## Running from source

```powershell
uv sync
uv run python -m app.desktop          # the desktop app (starts its own engine)
uv run python -m app.engine           # the engine alone (token written to <data>/engine.token)
```

Data directory: `%LOCALAPPDATA%\TraderInvestFX` (override with `TRADERINVEST_FX_HOME`). It
holds `config.json`, `fx.db` (journal + state), `logs/engine.log` and the instance lock.

Keyboard: **F11** full screen, **Ctrl+Q** quit. Click a station in the room to open its panel;
click a pair in *Mercado* to show it on the wall screen.

## Building the executable

```powershell
uv sync --group build
uv run --group build python packaging/build.py
```

Output: `dist/TraderInvestFX/TraderInvestFX.exe` (one-folder build) and
`dist/TraderInvestFX-<version>-win64-portable.zip`. The build script smoke-tests the frozen
engine (`--engine` prints `ENGINE_READY` and stops by itself without a client).

## IBKR paper — phase A (read-only)

`app/fx/ibkr` connects to the owner's **IBKR paper account** through IB Gateway/TWS running on
this machine (TWS API via `ib_async`). Phase A is read-only: quotes, M15 bars, account values
and forex positions. The adapter has no order method, connects with `readonly=True`, and
refuses (and disconnects from) any session whose accounts are not paper (`DU…`). The engine
still runs SIMULATION only; nothing here executes orders.

Owner setup (credentials stay in the Gateway, never in TraderInvest):

1. Start IB Gateway, set the Live/Paper switch to **Paper**, log in.
2. Gateway → Configure → Settings → API → Settings: enable socket clients, tick
   **Read-Only API**, socket port as shown (Gateway paper default 4002), trusted IP 127.0.0.1.
3. Run the diagnosis:

```powershell
uv run python -m app.fx.ibkr.probe            # uses ibkr.port from config.json (default 4002)
uv run python -m app.fx.ibkr.probe --port 7497 # TWS paper instead of IB Gateway
```

Exit codes: 0 ok, 2 no Gateway on that port, 3 refused (not a paper session), 4 connected but
no data (check forex permissions / market data sharing with the paper account).

## IBKR paper — phase B: experimental pilot (paper orders)

Authorized by the owner on 2026-10-18 for the **paper account only**. `app/fx/ibkr/pilot.py`
runs trained candidates (`app/fx/agents/trained.py`, chosen by the walk-forward tournament)
prospectively on the IBKR paper account. It is labelled **PILOTO EXPERIMENTAL** everywhere: no
candidate is statistically approved, and there is no promise of profit. The desktop engine is
unchanged (SIMULATION).

```powershell
uv run python -m app.fx.ibkr.pilot --once                          # dry-run, one cycle
uv run python -m app.fx.ibkr.pilot                                 # dry-run loop (30 s)
uv run python -m app.fx.ibkr.pilot --arm --confirm ARMAR-PAPER     # paper orders
uv run python -m app.fx.ibkr.pilot --self-check                    # offline registry check
```

From the portable build, use the same options with `TraderInvestFX.exe --paper-pilot`. Two
launchers sit next to the executable:
- `Piloto-Paper-Verificar.cmd`: an offline check.
- `Piloto-Paper-DryRun.cmd`: a dry-run (no orders).

Arming is only possible from a command line; the steps are in `PILOTO-PAPER-LEIA-ME.txt`. The
executable ships the selected registry, its report, its protocol and (when present) the
source evidence in `_internal/pilot/`; the guides are in `_internal/docs/`.
State lives in `%LOCALAPPDATA%\TraderInvestFX\pilot`. The pilot runs in its own console
window, separate from the GUI and its simulation.

- **Dry-run by default**: it computes and journals what it would do, and the API connection is
  read-only. Arming needs `--arm --confirm ARMAR-PAPER` on every start; it is never persisted.
  To send orders, the owner must untick **Read-Only API** in the Gateway **manually**. If the
  Gateway refuses an order because it is read-only, the pilot stops and says so.
- **Disarm**: create `data/fx/pilot/STOP`, press Ctrl+C, or let the kill switch engage.
- **Identity**: every managed account must be paper (`DU…`), checked each cycle and again
  immediately before each `placeOrder`. Ports 4001/7496 (usual live ports) are refused.
  Client id 52 by default (17 is the probe, 31 the downloader).
- **Data**: decisions use LIVE quotes only (market data type 1) with the ticker's own, recent
  timestamp. Signals use only closed H1/H4 bars, built from IBKR M15 bid/ask exactly as in the
  tournament, and only within 20 minutes of the bar close.
- **Isolation**: own journal and state in `data/fx/pilot/` (`pilot.sqlite`, `status.json`).
  Sizing and limits use a virtual capital of 10 000 EUR, not the paper balance. The pilot
  only manages positions it opened; a pair with any other position is skipped. A position
  changed outside the pilot engages the kill switch.
- **Risk**: the central risk manager in environment DEMO applies: LIVE data, 5 entries/day,
  daily locks, drawdown breaker, spread, rollover window, stop/target and R/R. At most one
  entry per cycle; candidates that disagree on a pair mean no trade.
- **Orders**: a market entry plus a take-profit limit and a stop held by IBKR (OCA, GTC). The
  deterministic client order id goes in `orderRef`.
  - The intention is journalled and persisted **before** sending.
  - The id is looked up before every send, so the same order is never sent twice.
  - An order without an answer is reconciled for up to 3 cycles. After that the kill switch
    engages, and nothing is resent blindly.
  - After an entry fills, both protective orders must be confirmed as working, and this is
    checked again every cycle. Otherwise the pilot raises an alarm and stops.
  - A partial entry: the pilot cancels the parent's remainder, waits for IBKR's
    confirmation, and records the FINAL filled total, which may have grown meanwhile. A
    complete fill counts as a normal entry. A final partial raises an alarm and stops the
    pilot, because the stop size no longer matches the exposure.
  - If the cancellation is not confirmed, or the pilot is not armed, the intention stays
    pending with the known exposure; the pilot raises an alarm and pauses. No flatten is ever
    sent blindly.
  - Quotes and the clock are refreshed after the slow history requests. An entry whose
    decision is older than 10 s just before `placeOrder` is not sent; exits are always
    allowed.
  - P&L and R are estimates: commissions are converted at the latest LIVE mid.
- **Exits**: stop or target at IBKR, a time stop at `max_hold` bars, and flat before the Friday
  close. A close first cancels the protective orders and waits for IBKR to confirm. If the
  stop or target filled in the meantime, no closing order is sent (no accidental reversal).
  If the cancellation is not confirmed, the pilot pauses.
- **Retirement**: closed paper trades, in net R and deduplicated by execution id, feed
  `retirement_decision`. A candidate is retired after a 5 R decline from its realized peak, or
  after 20 trades with a negative mean and a profit factor below 1. It is replaced by the next
  eligible candidate, or by NO TRADE.

Current state (2026-10-18): the first training run (`data/fx/training/run-20261018`) found
**no eligible candidate**, so the pilot runs in NO TRADE. A losing model is never promoted to
fill a slot.

## Agent research and validation

Agents v2 are validated on real IBKR history before they may trade (report:
[fx-agent-validation.md](fx-agent-validation.md)). With the IB Gateway open in Paper:

```powershell
uv run python -m app.fx.research.history --years 6    # M15 BID/ASK bars, read-only, resumable
uv run python -m app.fx.research.study calibrate       # development + validation, pre-registration
uv run python -m app.fx.research.study holdout         # the holdout, ONCE (refuses to re-run)
uv run python -m app.fx.research.study system          # whole product replayed on the holdout
uv run python -m app.fx.research.report                # writes docs/fx-agent-validation.md
```

Data and results live under `data/fx/` (not versioned). Only agents listed in
`app/fx/agents/calibrated.py` `APPROVED` may open positions.

**Result (2026-10-18): no agent passed the pre-registered out-of-sample rule**, so `APPROVED`
is empty: the agents analyse and show their proposals ("em observação") but the system opens
no position. See [fx-agent-validation.md](fx-agent-validation.md).

### Demonstration mode (SIMULATION only)

To see the agents trading in the interface, use **Ficheiro → Configuração →
"Demonstrar agentes NÃO APROVADOS na simulação"** (off by default; only enabled in
SIMULATION; takes effect immediately without resetting the account; untick it to stop new
demonstration entries). The equivalent setting in `%LOCALAPPDATA%\TraderInvestFX\config.json`
is:

```json
{"simulation": {"allow_unapproved_agents": true}}
```

> **Warning:** this lets agents that FAILED validation trade **synthetic** prices in
> SIMULATION. It demonstrates the interface and the risk rules; it says nothing about real
> performance. It has no effect in any other environment, and the Agents panel marks each
> agent "NÃO APROVADO — modo de demonstração".

## Arranque recusado (recuperação manual)

The engine refuses to start, before processing anything, when its configuration or persisted
data cannot be trusted. The desktop shows the code and does not restart the engine
automatically; the engine prints `ENGINE_ERROR startup:<code> <reason>` and exits with code 4.
Nothing is deleted, repaired or overwritten.

| Code | Meaning |
|---|---|
| `config_invalid` | `config.json` cannot be read or fails validation (e.g. environment `real`) |
| `environment_mismatch` | configuration, adapter and enabled environments disagree (only SIMULATION is enabled) |
| `state_unreadable` | `fx.db` is not a readable database |
| `state_corrupt` | a state document is malformed or incomplete, or state is missing while the journal has history |
| `journal_integrity` | the journal hash chain is broken, or shorter than the saved state expects |
| `account_mismatch` | the configured account currency or pairs differ from the persisted account |

Procedure:

1. Close TraderInvest FX and copy the whole data folder (`%LOCALAPPDATA%\TraderInvestFX`, or
   `TRADERINVEST_FX_HOME`) somewhere safe before changing anything.
2. `config_invalid`, `environment_mismatch`, `account_mismatch`: correct `config.json` (for
   example set `account_currency` back to the persisted account's currency) and start again.
3. `state_*` or `journal_integrity`: restore `fx.db` from a backup if one exists. Otherwise,
   to deliberately start a new simulated account, rename (do not delete) `fx.db` to keep it
   for diagnosis; the engine then creates a new account on the next start.
4. A persisted safety pause (`pause_reason = safety`) is not an error: inspect
   `logs/engine.log`, then resume explicitly with the Pause/Resume button (type `RETOMAR`).

## Tests

```powershell
uv run pytest tests/unit/fx -q
```

They cover the domain, calendar (DST, weekends, holidays), accounting (crosses, JPY, reversal),
the simulated broker (idempotency, rejections, ambiguous timeouts, partial fills, gapped stops,
margin close-out), the risk rules, the agents (including a no-look-ahead check), the
coordinator, the hash-chained store (tamper and deletion detection), the orchestrator
(lifecycle, limits, recovery after restart and crash), the engine protocol (authentication,
confirmations, shutdown, restart) and offscreen UI smoke tests.

## Known limitations

- The maximum drawdown breaker (kill switch at 20 % below the equity peak) blocks new
  entries only; it does not close open positions or cap losses exactly at that level.
- The v2 agents have **no demonstrated edge** on six years of IBKR data (holdout included):
  none is approved to open positions. New strategies must pass the same pre-registered
  protocol before `APPROVED` changes.

- Only the SIMULATION environment exists. A DEMO adapter (broker practice API) and a live data
  provider are not implemented: they need a chosen provider and the owner's own practice
  account and credentials.
- Swap/overnight financing, guaranteed stops, hedging accounts and pending (limit/stop) entry
  orders are not modelled.
- The agents are simple, transparent baselines. Their edge on real data has **not** been
  validated; the repository's statistical-validation tools should be applied to recorded FX
  data before any performance claim.
- The synthetic market's regimes are random by construction; profits or losses in simulation
  are not evidence of anything.
