# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/),
versioning: [SemVer](https://semver.org/).

## [Unreleased]

## [0.3.0] — 2026-10-07 — Phase 3: Candlestick Engine

### Added
- `app/candles`: `CandlestickEngine`, vectorised candle geometry (ratios, gap, relative volume,
  past-only trend context), 20 deterministic pattern detectors with explicit thresholds in
  `CandleConfig`, geometric `strength`, `context_requirements_met`, engine version and config
  fingerprint.
- Read-only loader of closed bars (raw OHLC) and `python -m app.candles.cli scan`.
- 73 new tests (149 total): every pattern positive/negative/borderline, zero-range, invalid
  rows, insufficient history, multiple patterns per candle, timezone/input checks,
  determinism, point-in-time truncation and future-perturbation tests (with a leaky control),
  SPY 2020 regression snapshot.
- `docs/candlestick-engine.md`, ADR-0010, ADR-0011.

### Fixed
- Phase 2 files under `app/data/` and `tests/unit/data/` had never been linted/formatted by
  ruff (ruff honours `.gitignore`, which ignored `data/` until the end of Phase 2). They are now
  formatted and lint-clean: line wrapping, one nested `if` merged (equivalent logic), two
  data-quality description strings reworded. No behaviour change; all Phase 2 tests unchanged.

### Not included (by design)
- No persistence of observations, signals, indicators, strategies, backtests or ML.

## [0.2.0] — 2026-10-07 — Phase 2: Market Data Foundation

### Added
- `DataProvider` interface and `YFinanceProvider` (daily; `auto_adjust=False`, `repair=False`),
  injectable fetcher for offline tests.
- XNYS trading calendar (`exchange_calendars`), early-close detection, `market_sessions` upsert.
- Timezone/session normalization: daily bars stamped with session open (UTC).
- Data-quality validation (16 checks) with exclude-vs-flag policy; issues persisted to
  `data_quality_events`.
- Idempotent persistence of raw + adjusted prices with revision detection and adj_close noise
  tolerance.
- `ingestion_runs` table (migration `c4cb3e615291`) and `data_quality_events.ingestion_run_id`.
- Instrument registry (SPY) with provider symbol mapping.
- CLI: `python -m app.data.cli ingest`.
- 48 new tests (76 total); `docs/market-data.md`; ADR-0009.

### Data
- First ingestion: 8,479 SPY daily bars (1993-01-29 → 2026-10-06), 0 excluded, 0 missing sessions,
  8 flagged outliers (kept).

## [0.1.0] — 2026-10-07 — Phase 1: Foundation

### Added
- Project structure (`app/`, `migrations/`, `tests/`, `docs/`), Python 3.12 + uv, `uv.lock`.
- Configuration from environment variables via pydantic-settings; `.env.example`.
- Execution safety: `TradingMode` limited to `disabled`/`paper` (default `disabled`), real-money
  order guard, startup check.
- Structured JSON logging (structlog) with UTC timestamps, request IDs and secret redaction.
- PostgreSQL 16 via Docker Compose (localhost-only binding, UTC).
- SQLAlchemy 2 models and initial Alembic migration: `instruments`, `provider_symbols`,
  `price_bars`, `market_sessions`, `data_quality_events`.
- FastAPI app with `GET /health` performing a real database check (200 / 503) and reporting the
  schema revision.
- Unit and integration tests (28), ruff, mypy (strict).
- README, `docs/architecture.md`, `docs/decisions.md` (ADR-0001…0008, including the 4H bar
  methodology).

### Not included (by design)
- Market data download, analysis, signals, trading logic, broker integration, dashboard.
