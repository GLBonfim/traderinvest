# Quant Research Platform

A research platform for **market analysis and algorithmic trading research**, starting with the
S&P 500 (via SPY) and designed to extend to other instruments and asset classes.

> **This is a research tool, not a money-making machine.** It makes no claims of profitability.
> Its purpose is to find out — with out-of-sample evidence — whether a statistical edge exists,
> and to say `NO STATISTICAL EDGE FOUND` or `INSUFFICIENT EVIDENCE` when it does not.
> **Real-money execution is disabled and blocked in code.**

## Status

| Phase | Scope | Status |
|---|---|---|
| 0 | Environment audit | ✅ Done |
| 1 | Foundation: structure, config, PostgreSQL, migrations, logging, health check, tests | ✅ Done |
| 2 | Market data (yfinance prototype, SPY daily), validation, storage | ⏳ Awaiting approval |
| 3–15 | Candles, price action, indicators, regimes, strategies, backtesting, validation, ML, risk, paper trading, dashboard, alerts, broker | Not started |

There is currently **no market data, no analysis, no signals and no trading logic** in this repository.

## Principles

- Decisions are based on historical, point-in-time evidence — never on guesses.
- No look-ahead bias, no data leakage, no random train/test splits on time series.
- A candlestick pattern is **not** a signal. `NO TRADE` is a valid decision.
- Data problems are logged, never silently fixed.
- Bad results are reported, not hidden.

## Architecture (summary)

```
app/
├── api/        FastAPI app (only /health for now)
├── core/       settings (env vars), structured logging, execution safety guard
└── database/   SQLAlchemy 2 models + session
migrations/     Alembic migrations
tests/          unit + integration (PostgreSQL) tests
docs/           architecture, decisions (ADRs), methodology
```

Modules for later phases (`data/`, `candles/`, `price_action/`, …) are created only when their
phase starts. See [docs/architecture.md](docs/architecture.md) and
[docs/decisions.md](docs/decisions.md).

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Docker Desktop (for PostgreSQL 16)

## Setup

```bash
# 1. Configuration (never commit .env)
cp .env.example .env
#    then set POSTGRES_PASSWORD to a long random value

# 2. Dependencies
uv sync

# 3. Database
docker compose up -d db
docker compose ps            # wait for "healthy"

# 4. Migrations
uv run alembic upgrade head

# 5. API
uv run uvicorn app.api.main:app --reload
curl http://127.0.0.1:8000/health
```

`/health` returns `200` with `"database": {"status": "ok"}` when PostgreSQL is reachable, and
`503` with `"status": "degraded"` when it is not.

## Tests and quality checks

```bash
uv run pytest                    # all tests (integration tests skip if DB is down)
uv run pytest -m "not integration"
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

## Configuration

All settings come from environment variables (or `.env`). See [.env.example](.env.example).

| Variable | Default | Notes |
|---|---|---|
| `APP_ENV` | `development` | `development` / `test` / `production` |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `json` | `json` or `console` |
| `TRADING_MODE` | `disabled` | `disabled` or `paper`. There is no `live` value. |
| `POSTGRES_*` | — | `POSTGRES_PASSWORD` is required |

## Data sources

| Provider | Role | Status |
|---|---|---|
| yfinance | Prototyping only (unofficial, ToS-restricted, ~730 days of 1h history) | Phase 2 |
| Massive (Polygon) | Intended primary OHLCV provider | Future |
| Alpha Vantage / FMP / FRED | Macro, calendars, point-in-time data | Future |

## Methodology (planned)

Backtests will use temporal splits, walk-forward validation, out-of-sample testing, transaction
costs, slippage and comparison to buy & hold. Every component (candles, indicators, regimes)
must earn its place through ablation testing. See [docs/architecture.md](docs/architecture.md).

## Risk management (planned)

Fixed-fractional and volatility-targeted sizing, max exposure, max daily loss, max drawdown and
mandatory no-trade conditions. Paper trading only until full validation.

## Limitations

- No market data or analysis exists yet.
- yfinance is unsuitable for production and limits intraday history.
- Past performance in a backtest does not predict future results.

## GitHub

The repository is local for now. To connect a **private** GitHub repository:

```bash
# create an empty private repo on github.com (no README/license), then:
git remote add origin git@github.com:<user>/<repo>.git
git push -u origin main
```

Before every push, confirm that `git status` shows no `.env` or data files.
