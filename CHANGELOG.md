# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/),
versioning: [SemVer](https://semver.org/).

## [Unreleased]

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
