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
| 2 | Market data: provider interface, yfinance SPY daily, NYSE calendar, validation, storage | ✅ Done |
| 3 | Candlestick Engine: candle geometry + 20 pattern detectors (observations, not signals) | ✅ Done |
| 4 | Price Action Engine: swings, structure, zones, breakouts, retests, rejections, ranges | ✅ Done |
| 5 | Technical indicators: SMA, EMA, MACD, RSI, Stochastic, ROC, ATR, Bollinger, realized vol, relative volume, OBV | ✅ Done |
| 6 | Market regimes: trend, volatility (causal percentile), momentum, participation, composite | ✅ Done |
| 7 | Baseline strategies: buy & hold, SMA trend, SMA crossover, RSI, price-action trend, regime trend (LONG/FLAT states only) | ✅ Done |
| 8 | Backtesting engine: next-session-open execution, explicit costs, gross/net, benchmarks (price & total return), metrics | ✅ Done |
| 9 | Statistical validation: moving-block bootstrap intervals, paired comparisons, Holm/BH, cost sensitivity, slices | ✅ Done |
| 10–15 | indicators, regimes, strategies, backtesting, validation, ML, risk, paper trading, dashboard, alerts, broker | Not started |

The platform can ingest and validate SPY daily bars and describe candle geometry and
candlestick patterns, describe market structure and price-action events, and compute technical
indicators and descriptive market regimes. Patterns, events, indicator values and regime labels
are **observations/features, not trading signals or predictions**. Six fixed baseline strategies
turn them into hypothetical LONG/FLAT states; **baseline strategies are research benchmarks, not
evidence of profitability**. A research backtester simulates them under explicit execution and
cost assumptions; **backtest results are historical simulations, not evidence of future
profitability**. Phase 9 quantifies their uncertainty (block bootstrap, multiple-testing
control): on SPY the data **do not distinguish** the risk-adjusted performance of any timing
baseline from buy & hold — an exploratory, not conclusive, result. There are
**no indicators, no signals, no strategies and no trading logic** yet.

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
├── data/       providers (DataProvider, yfinance), NYSE calendar, normalization,
│               validation, ingestion, CLI
├── candles/    Candlestick Engine: geometry, pattern detectors, loader, CLI
├── price_action/ Price Action Engine: swings, structure, zones, events, outcomes, ranges
├── indicators/ Technical Indicator Engine: transparent formulas, warm-up catalog, CLI
├── regimes/    Market Regime Engine: trend/volatility/momentum/participation + composite
├── strategies/ Six fixed baseline rules -> LONG/FLAT/INSUFFICIENT_DATA states (no orders)
├── backtest/   Research backtester: next-open fills, costs, equity, metrics, benchmarks
├── validation/ Block-bootstrap uncertainty, paired comparisons, Holm/BH, cost sensitivity
└── database/   SQLAlchemy 2 models + session
migrations/     Alembic migrations
tests/          unit + integration (PostgreSQL) tests
docs/           architecture, decisions (ADRs), methodology
```

Modules for later phases (…) are created only when their
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

Ingest SPY daily history (yfinance, prototype only):

```bash
uv run python -m app.data.cli ingest --symbol SPY --start 1993-01-01   # --end defaults to today
```

Re-running is idempotent. See [docs/market-data.md](docs/market-data.md) for price semantics,
session normalization and the data-quality checks.

Describe candlestick patterns on stored bars (descriptive counts, no predictive claim):

```bash
uv run python -m app.candles.cli scan --symbol SPY
```

Definitions and point-in-time guarantees: [docs/candlestick-engine.md](docs/candlestick-engine.md).

Describe market structure and price-action events (observations; outcomes kept separate):

```bash
uv run python -m app.price_action.cli scan --symbol SPY
```

See [docs/price-action-engine.md](docs/price-action-engine.md) (swing confirmation latency,
zones, breakout/retest/rejection/sweep rules, observation vs outcome).

Technical indicators (latest values; descriptive features only):

```bash
uv run python -m app.indicators.cli latest --symbol SPY
uv run --with TA-Lib python scripts/crossvalidate_indicators.py   # optional cross-check
```

Formulas, warm-up and edge cases: [docs/technical-indicators.md](docs/technical-indicators.md).

Market regimes (descriptive labels, not signals):

```bash
uv run python -m app.regimes.cli describe --symbol SPY
```

Definitions, causal percentile and composite table: [docs/market-regimes.md](docs/market-regimes.md).

Baseline strategy states (no returns, no performance — that is Phase 8):

```bash
uv run python -m app.strategies.cli run --symbol SPY
```

Rules, timing (`observed_at` / `effective_at`) and guarantees:
[docs/baseline-strategies.md](docs/baseline-strategies.md).

Backtest diagnostic of the baselines (hypothetical, explicit assumptions):

```bash
uv run python -m app.backtest.cli baseline --symbol SPY [--start 2000-01-01 --end 2009-12-31]
```

Execution, costs, metrics and the baseline diagnostic: [docs/backtesting.md](docs/backtesting.md).

Statistical validation (exploratory; ~30 s; optional CSV output in git-ignored `data/`):

```bash
uv run python -m app.validation.cli run --symbol SPY --out data/validation
```

Methodology, multiple-testing family and results: [docs/statistical-validation.md](docs/statistical-validation.md).

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
| yfinance | Prototyping only (unofficial, ToS-restricted, ~730 days of 1h history) | SPY daily ✅ |
| Massive (Polygon) | Intended primary OHLCV provider | Future |
| Alpha Vantage / FMP / FRED | Macro, calendars, point-in-time data | Future |

## Methodology

Backtests execute at the next session open with explicit costs and slippage, report gross and
net results, and compare against price-return and total-return buy & hold (implemented,
Phase 8). Phase 9 adds moving-block bootstrap uncertainty intervals, paired comparisons with a
pre-declared test family and Holm/BH corrections, predefined cost scenarios and fixed
chronological slices. No parameter has been fitted on any slice. Every component (candles, indicators, regimes)
must earn its place through ablation testing. See [docs/architecture.md](docs/architecture.md).

## Risk management (planned)

Fixed-fractional and volatility-targeted sizing, max exposure, max daily loss, max drawdown and
mandatory no-trade conditions. Paper trading only until full validation.

## Limitations

- Only SPY daily bars from yfinance.
- Candlestick patterns, price-action events and indicators are unvalidated features;
  thresholds and periods are conventions.
- Indicators use raw (dividend-unadjusted) closes.
- Regimes have no market-breadth dimension (no point-in-time constituent data yet).
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
