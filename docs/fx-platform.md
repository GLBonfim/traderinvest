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
| `account_mismatch` | the configured account currency differs from the persisted account |

Procedure:

1. Close TraderInvest FX and copy the whole data folder (`%LOCALAPPDATA%\TraderInvestFX`, or
   `TRADERINVEST_FX_HOME`) somewhere safe before changing anything.
2. `config_invalid`, `environment_mismatch`, `account_mismatch`: correct `config.json` (for
   example set `account_currency` back to the persisted account's currency) and start again.
3. `state_*` or `journal_integrity`: restore `fx.db` from a backup if one exists. Otherwise,
   to deliberately start a new simulated account, rename (do not delete) `fx.db` to keep it
   for diagnosis; the engine then creates a new account on the next start.
4. A persisted safety pause (`pause_reason = safety`) is not an error: inspect
   `logs/engine.log`, then resume explicitly with the Pause/Resume button.

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
