# Research & Paper-Trading Dashboard (Phase 13)

> **Daily research system** — a research and monitoring interface over locally ingested daily
> data. It is **not** a live-trading terminal, it is not real-time, and nothing in it is
> investment advice. It has no real-money execution capability. Its only mutating actions are
> **PAPER SIMULATION** actions on local paper accounts; paper trading is a local simulation that
> does not transmit orders to any brokerage or market.

Code: `app/dashboard/` · Streamlit + Plotly · ADR-0022.

## Starting it

Required local services: PostgreSQL with ingested SPY bars (`docker compose up -d db`; the
dashboard never fetches market data).

```bash
docker compose up -d db
uv run python -m app.data.cli ingest --symbol SPY --start 1993-01-01   # refresh data (optional)
uv run streamlit run app/dashboard/streamlit_app.py
# -> http://127.0.0.1:8501
```

`.streamlit/config.toml` binds the server to `127.0.0.1` only and disables Streamlit usage
statistics (no outbound network calls). The entry point calls `assert_safe_trading_mode` before
rendering anything.

## Architecture

```
domain / research engines (app.candles … app.paper)          ← all financial logic
        ↓
app/dashboard/services/   market · analysis · research · paper · explain · health
        │                 pure, deterministic, no Streamlit/Plotly import (tested)
        ↓
app/dashboard/charts.py   Plotly figure builders (plot what they are given)
app/dashboard/ui/         context.py (cache + sidebar) · pages.py (one function per page)
app/dashboard/streamlit_app.py   navigation entry point
```

No calculation is duplicated in the UI: every value is an engine output or a direct selection of
one (integration tests compare the dashboard with each engine on the full SPY history).

## Sections

| Page | Content | Source |
|---|---|---|
| Market Overview | latest completed session, raw OHLC, volume, previous close, change, exchange-calendar status, dataset range, bars, provider, last ingestion run, stale-data warning, candlestick chart, **session summary** | `price_bars`, `ingestion_runs`, XNYS calendar, all engines |
| Technical Analysis | candlestick chart with optional SMA/EMA 20/50/200 and Bollinger overlays (only SMA200 by default), Candlestick Engine panel for the session, indicator values with warm-up/undefined status, separate MACD, RSI, Stochastic, ROC, ATR, realized-vol, Bollinger-width, relative-volume, OBV charts | Phases 3, 5 |
| Market Structure | structure, trend quality, swing labels, support/resistance/range, event counts, chart with confirmed swings, events and nearest zones | Phase 4 |
| Market Regime | trend / volatility / momentum / participation / composite with age, change flag, previous label; underlying measures; volatility-percentile history | Phase 6 |
| Strategy Research | "Research Strategy States": state, state in effect, observed/effective timestamps, last transition, reason, version for the six baselines | Phase 7 |
| Backtesting | unranked, sortable comparison table, gross/net equity curves, drawdown curves, trade-return distribution, price- vs total-return benchmark notes | Phase 8 |
| Statistical Validation | project conclusion, the 30 pre-declared formal tests with raw/Holm/BH p-values and exact status, bootstrap intervals by slice and metric, cost sensitivity, descriptive comparisons | Phase 9 (persisted) |
| Machine Learning | verdict, models, feature count, target, split, classification metrics, AUC intervals, edge rule, predicted-probability distribution, calibration (reliability) on TEST | Phase 10 (persisted) |
| Risk Management | Strategy request → RiskManager → approved exposure for the selected strategy/scenario/session: sizing method, volatility target, ATR budget, drawdown, lock/stop status, reasons; requested vs approved history; all six scenarios for the strategy | Phase 11 |
| Paper Trading | account (cash, market value, equity, realized/unrealized/total P&L, costs, exposure), position (quantity, average cost incl. costs, mark, value), current state (strategy state, requested/approved exposure, pending entry/exit/rebalance, next effective session), recent ledger (decisions, orders, fills, reconciliation, snapshots) | Phase 12 |
| Alerts & Monitoring | monitoring table (database, data freshness, ingestion, paper ledgers, alert store, version), alert counters, filters (severity, source, strategy, account, date range), alert table with delivery status, alert detail with the structured payload; explicit "evaluate now" button (writes to the alert store only) | Phase 14 (docs/alerts.md) |
| Operations | latest completed / next eligible / ingested / paper sessions, checkpoint, scheduler heartbeat, lock, latest alert evaluation, last run with stage durations, recent runs, operational metrics; **Dry run** and **PAPER SIMULATION / LOCAL OPERATIONS: run pending sessions now** | Phase 14.5 (docs/operations.md) |
| Broker Sandbox | armed / kill switch / trading mode / credentials present / linked accounts, dry-run preview, recorded sandbox orders; one control: **engage kill switch** (arming, releasing and submitting are CLI-only) | Phase 15 (docs/broker-sandbox.md) |
| System / Data Health | database, schema revision vs head, app version, git commit, dataset range, bars, data-quality events, paper accounts with ledger verification, safety mode | — |

The sidebar selects the **session** (as of its close; a non-session date shows the latest
session on or before it, stated explicitly), the **research strategy** and the **risk scenario**
used by the session summary, the risk page and the paper look-up.

### Point-in-time display

For a historical session T: candle rows of T; price-action state of T; swings only once
`confirmed_at ≤ T` (a pivot is never drawn as known before it was confirmed); events only from
`available_at`; support/resistance from the state of T (the engine's "zones as of the last bar"
table is not used for history); indicator, regime and strategy rows of T. Tested: the view at T
from the full history equals the view from a history ending at T.

### Session summary (explanation panel)

`services/explain.py` fills fixed templates with fields of the engine views — price, candlestick,
structure, trend, volatility, momentum, participation, strategy state, risk-approved exposure,
paper account — and lists the source fields of every line. It is deterministic and contains no
recommendation wording (tested). **No language model is used anywhere in the dashboard** and
none decides direction, exposure, stops or size.

## Caching

| What | Cache | Key |
|---|---|---|
| stored bars | `st.cache_data` | dataset identity + configuration key |
| engine outputs (Phases 3–7), Phase 8 backtests, risk overlays, paper inputs | `st.cache_resource` (read-only use) | same |
| Phase 9 validation, Phase 10 ML | files under git-ignored `data/dashboard/research/` | validation: dataset identity + validation config fingerprint; ML: the Phase 10 experiment id (ML config + OHLCV fingerprint) |
| paper accounts | **never cached** | re-read and ledger re-verified on every rerun |

Dataset identity = app version + instrument + provider + timeframe + bar count + first/last bar
+ latest `ingested_at` (one cheap query per rerun). Configuration key = fingerprints of the
strategy engine (incl. indicators, price action, regimes), backtest, validation, ML and every
risk scenario. A new ingestion, version or configuration change therefore never serves old
results.

**Long jobs are never started by opening a page.** Validation (~50 s) and the ML experiment
(~95 s, trains the two pre-declared models on TRAIN only) run only when the user presses their
explicitly labelled button; otherwise the page says the results are missing for this
dataset/configuration.

## Paper controls (PAPER SIMULATION)

The only mutating controls, both labelled `PAPER SIMULATION` and delegated to `app.paper`:

- create a local paper account (strategy, pre-declared risk scenario, first session) and process
  the available completed sessions;
- process available completed sessions for the selected account (idempotent; already processed
  sessions have no effect).

Plus select account and refresh. Accounts live in `data/paper/accounts/` (git-ignored). A
corrupt or edited ledger is reported and nothing is repaired. There is no concept of a live
account, broker account or real order.

## Safety restrictions

- `TradingMode` remains {`disabled`, `paper`}; `refuse_real_money_order()` is unchanged.
- No broker connector, credentials, order API, websocket or "go live" control (tested by source
  inspection); `app/dashboard` imports no network, broker, data-provider or LLM module (AST test);
  services import neither Streamlit nor Plotly.
- No secret is displayed: database errors are shown by exception type; the system page shows
  only the trading mode (integration test checks the configured password never appears).
- Colours are muted and describe candle direction only; nothing is coloured as buy/sell.

## Performance (local machine, SPY 8,479 sessions)

Server start ≈ 4 s. First page load computes the engines once per dataset: Market Overview
≈ 19 s cold, 0.2 s warm; Technical Analysis / Market Structure / Market Regime / Strategy Research
< 1 s cold, ≤ 0.4 s warm; Backtesting ≈ 13 s cold (Phase 8 suite), ≈ 3 s warm (plotting 8 ×
8,479 points); Risk Management ≈ 5 s cold per strategy (six scenarios), 0.1 s warm; Paper Trading
≈ 9 s cold (paper inputs), ≈ 0.1 s warm plus ledger verification; Statistical Validation / ML
≈ 0.5 s (reading persisted results). Explicit recomputation: validation ≈ 48 s, ML ≈ 93 s.

## Known limitations

- Daily, end-of-day research data only; one instrument (SPY); no intraday or real-time view.
- Persisted validation/ML results are recomputed only on request; after a new ingestion the page
  states that they are missing for the new dataset.
- Cached engine bundles are held in memory per dataset (≈ a few hundred MB for SPY).
- The paper "create" control uses the pre-declared risk scenarios only; custom configurations
  are not offered.
- Streamlit reruns the page script on every interaction; heavy charts (full-history curves) take
  a few seconds to serialise.
- No authentication: the server is bound to localhost for single-user local use.
