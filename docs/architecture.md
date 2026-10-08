# Architecture

Status: **Phase 14 — Alerts & Monitoring**. This document describes what exists today and the intended
direction. Components for later phases are listed as *planned* and do not exist in code yet.

## Goals

A reproducible, testable, evidence-driven research platform that can:

1. ingest and validate market data without silently altering it;
2. analyse market context (regime, structure, levels, candles, volume, volatility);
3. test hypotheses with temporal, out-of-sample methodology;
4. output `LONG` / `SHORT` / `NO TRADE` decisions with probabilities, risk and invalidation;
5. paper-trade, and only much later (with explicit approval) connect to a broker.

## Current components

```
┌──────────────────────────── host (uv, Python 3.12) ────────────────────────────┐
│                                                                                │
│  app.api.main  (FastAPI)                                                       │
│     ├── middleware: request_id + structured access log                        │
│     └── GET /health ──► app.database.session.get_engine() ──┐                  │
│                                                              │                 │
│  app.core.config   Settings from env / .env (pydantic)       │                 │
│  app.core.logging  structlog JSON, UTC, secret redaction     │                 │
│  app.core.safety   real-money execution guard                │                 │
│  app.database      SQLAlchemy 2 models + Base                │                 │
│  app.data          DataProvider, YFinanceProvider, XNYS      │                 │
│                    calendar, normalization, validation,      │                 │
│                    ingestion, CLI  (see market-data.md)      │                 │
│  app.candles       CandlestickEngine: geometry + detectors,  │                 │
│                    on demand, no persistence (see            │                 │
│                    candlestick-engine.md)                    │                 │
│  app.price_action  PriceActionEngine: swings (confirmation   │                 │
│                    latency), structure, zones, events,       │                 │
│                    outcomes, ranges (price-action-engine.md) │                 │
│  app.indicators    IndicatorEngine: SMA/EMA/MACD/RSI/Stoch/   │                 │
│                    ROC/ATR/Bollinger/RV/RelVol/OBV           │                 │
│                    (technical-indicators.md)                 │                 │
│  app.regimes       RegimeEngine: composes price action +     │                 │
│                    indicators into descriptive regimes       │                 │
│                    (market-regimes.md)                       │                 │
│  app.strategies    StrategyEngine: 6 fixed baselines ->      │                 │
│                    LONG/FLAT states, calendar timing         │                 │
│                    (baseline-strategies.md)                  │                 │
│  app.backtest      Backtester: next-open fills, costs, gross/│                 │
│                    net equity, metrics, benchmarks           │                 │
│                    (backtesting.md)                          │                 │
│  app.validation    Validator: block bootstrap, paired tests, │                 │
│                    Holm/BH, cost sensitivity, slices         │                 │
│                    (statistical-validation.md)               │                 │
│  app.ml            MLExperiment: point-in-time features,     │                 │
│                    chronological split, LR/RF, Phase 8/9     │                 │
│                    evaluation (machine-learning.md)          │                 │
│  app.risk          RiskOverlay: requested -> approved        │                 │
│                    exposure, [0,1] accounting reusing Phase 8│                 │
│                    (risk-management.md)                      │                 │
│  app.paper         PaperTrader: strategy -> RiskManager ->   │                 │
│                    Instruction -> PaperBroker (local         │                 │
│                    simulation), replay + incremental store,  │                 │
│                    hash-chained ledger (paper-trading.md)    │                 │
│  app.alerts        deterministic alerts & monitoring:        │                 │
│                    rules -> hash-chained store -> console /  │                 │
│                    dashboard / optional webhook (alerts.md)  │                 │
│  app.dashboard     Streamlit research dashboard: services    │                 │
│                    (pure) -> charts -> pages; read-only,     │                 │
│                    PAPER SIMULATION controls (dashboard.md)  │                 │
│  migrations/       Alembic (URL from env, never from .ini)   │                 │
└──────────────────────────────────────────────────────────────┼─────────────────┘
                                                               │ 127.0.0.1:5432
                                              ┌────────────────▼───────────────┐
                                              │ Docker: postgres:16-alpine     │
                                              │ volume pgdata, TZ=UTC          │
                                              └────────────────────────────────┘
```

## Dependency direction (Phases 3–12)

Imports only point upstream; there are no cycles (checked with `git grep` over `app/`):

```
candles ← price_action, indicators ← regimes ← strategies ← backtest ← validation ← ml
                                                           backtest, validation ← risk ← paper
```

`app.paper` depends on `backtest` (costs, metrics, session timing), `risk` (RiskManager,
`plan_order`/`apply_order`), `strategies`, `indicators`, `data.calendar` and `core.safety`;
nothing imports `app.paper`. It has no network or broker imports (AST-checked test).

`app.alerts` reads engine outputs, paper ledgers (read-only) and database rows and imports no
trading module; `app.dashboard` shows its store and health. `app.dashboard` sits on top of
everything and is imported by nothing except `app.alerts.sources` (read-only market helpers); its `services` import no
UI library, and the package imports no network, broker, data-provider or LLM module.

## Domain model: separating what is traded from where data comes from

| Concept | Example | Where |
|---|---|---|
| Underlying | S&P 500 | `instruments.underlying` |
| Instrument | SPY (ETF) | `instruments` row |
| Exchange | ARCX (NYSE Arca, ISO 10383 MIC) | `instruments.exchange` |
| Provider ticker | `SPY` on yfinance, `SPY` on Massive, `^GSPC` for the index | `provider_symbols` |
| Data provider | yfinance, massive, alpha_vantage | `price_bars.provider`, `provider_symbols.provider` |

Strategies will reference an **instrument**, never a provider ticker or API. Bars from different
providers can coexist for the same instrument (unique key includes `provider`), which enables
cross-provider reconciliation.

## Schema (migrations `d77968d9c70f`, `c4cb3e615291`)

| Table | Purpose |
|---|---|
| `instruments` | Tradable instruments and their underlying, venue, currency, timezone |
| `provider_symbols` | Provider-specific tickers for an instrument |
| `price_bars` | OHLCV bars per (instrument, provider, timeframe, ts). `ts` = bar open, UTC. `is_closed` distinguishes forming vs closed bars. `adj_close` kept alongside raw prices |
| `market_sessions` | Regular session open/close per exchange calendar and date |
| `ingestion_runs` | One row per ingestion: request, `as_of`, provider version, counts, status |
| `data_quality_events` | Every detected data problem, with the action taken and its run |

Rules:

- **All timestamps are `TIMESTAMPTZ` in UTC.** DB sessions are forced to `timezone=UTC`.
- **Numeric, not float**, for prices (`NUMERIC(18,6)`) and volume (`NUMERIC(28,8)`, room for
  fractional crypto volume later).
- **The database enforces structure, not market sanity.** Constraints cover uniqueness and
  enumerations (timeframe, asset class, severity). Checks such as `high >= low` belong to the
  data-validation layer (Phase 2), which must record a `data_quality_events` row instead of
  silently rejecting or "fixing" data.
- Deterministic constraint names (naming convention in `app/database/base.py`) keep migrations
  stable across environments.
- Every model change requires a migration; `tests/integration` runs `alembic check` to enforce it.

## Configuration

All configuration comes from environment variables, optionally loaded from `.env` (git-ignored).
`.env.example` documents every variable. Secrets are `SecretStr` and the DB URL is a SQLAlchemy
`URL` object, so neither prints the password in `repr`/`str` or logs.

## Logging

structlog → JSON lines on stdout, one schema for app and stdlib loggers (uvicorn, alembic):
`timestamp` (ISO-8601 UTC), `level`, `logger`, `event`, plus context (`request_id`, ...).
Keys containing `password`, `secret`, `token`, `api_key`, `authorization` are redacted.
Set `LOG_FORMAT=console` for human-readable output during development.

## Execution safety

See ADR-0006. In short: `TradingMode` has only `disabled` and `paper`; `live` cannot be configured;
`refuse_real_money_order()` always raises; the API exposes no order endpoints (tested).

## Testing strategy

| Layer | Location | Needs DB |
|---|---|---|
| Unit | `tests/unit` | No |
| Integration | `tests/integration` (marker `integration`) | Yes — a throwaway `<db>_test` database is created and dropped; the dev DB is untouched. Skipped if PostgreSQL is down |

Planned test families (later phases): look-ahead/leakage tests (features at `t` must be invariant
to any data at `t+1..`), point-in-time tests, backtest accounting, transaction costs.

## Planned module layout

Created only when the corresponding phase starts:

```
app/
├── features/
├── signals/       SignalEngine, DecisionEngine
├── execution/     Broker interface (paper broker exists in app/paper; no real broker)
└── dashboard/     Streamlit (exists: app/dashboard, Phase 13)
```

## Known limitations

- No signals or trading logic exist. Market data: SPY daily from yfinance only.
- Candlestick, price-action, indicator and regime observations and strategy states are
  computed on demand and not persisted (ADR-0010, ADR-0012, ADR-0014, ADR-0015, ADR-0016).
- Backtest results are computed on demand and not persisted yet (experiment tracking: later).
- No real execution exists. Paper trading (Phase 12) is a local simulation; its state and
  ledger are files under the git-ignored `data/paper/`, not database tables.
- The app runs on the host; only PostgreSQL is containerised (no app Dockerfile yet).
- No CI pipeline yet.
- `/health` can take up to ~2× `DB_CONNECT_TIMEOUT_S` to report `503` when the DB is down
  (connection attempt + pool pre-ping).
- uvicorn prints its first two boot lines before the app's logging is configured, so those two
  lines are plain text rather than JSON.
