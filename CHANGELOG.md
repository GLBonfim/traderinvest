# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/),
versioning: [SemVer](https://semver.org/).

## [Unreleased]

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
