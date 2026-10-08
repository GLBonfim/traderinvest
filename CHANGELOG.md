# Changelog

All notable changes are documented here. Format: [Keep a Changelog](https://keepachangelog.com/),
versioning: [SemVer](https://semver.org/).

## [Unreleased]

## [0.14.0] — 2026-10-08 — Phase 14: Alerts & Monitoring

### Added
- `app/alerts`: deterministic, downstream-only alerting — 33 event types over data (stale,
  missing session, ingestion failure, provider failure, quality events), market regimes,
  strategy-state transitions, risk interventions (reduced, blocked, drawdown warning, lock
  cooldown start/end, stop), paper simulation events (entry/exit/rebalance scheduled, fill,
  rejected order, account/state/ledger errors), system and safety invariants; INFO / WARNING /
  CRITICAL; transition and episode semantics (no alert storms); point-in-time `alert_id`.
- Hash-chained local store `data/alerts/` (alerts, deliveries, versioned state), console and
  dashboard channels, optional generic webhook (`ALERT_WEBHOOK_URL`) with bounded,
  non-blocking retries; lightweight monitoring (database, freshness, ingestion, paper ledgers,
  alert store, safety, version) and operational counters; CLI `python -m app.alerts.cli`
  (`run`, `list`, `status`, `verify`, `test-channel`).
- Dashboard page "Alerts & Monitoring" (filters, detail with structured payload, delivery
  status, health).
- 34 alert unit tests and 4 SPY integration tests (bar → engines → strategy → risk → paper →
  alert, timing checked against the ledger and engines).
- `docs/alerts.md`, ADR-0023; `.env.example` documents the optional webhook variables.

## [0.13.0] — 2026-10-07 — Phase 13: Research & Paper-Trading Dashboard

### Added
- `app/dashboard`: Streamlit dashboard (daily research system; not a live-trading terminal) with
  11 sections — Market Overview, Technical Analysis, Market Structure, Market Regime, Strategy
  Research, Backtesting, Statistical Validation, Machine Learning, Risk Management, Paper
  Trading, System / Data Health — over pure services (`market`, `analysis`, `research`, `paper`,
  `explain`, `health`) and Plotly chart builders.
- Point-in-time historical views (confirmation latency respected), stale-data detection from the
  XNYS calendar, deterministic template-based session summary with source fields (no LLM).
- Caching keyed by dataset identity + configuration fingerprints; paper accounts never cached;
  Phase 9/10 results persisted in git-ignored `data/dashboard/research/` and recomputed only by
  explicit, labelled actions.
- Paper controls (PAPER SIMULATION only): create a local account, process available sessions.
- `.streamlit/config.toml`: localhost-only server, usage statistics disabled.
- Dependencies: streamlit, plotly.
- 31 dashboard unit tests (mapping, freshness, point-in-time views, explanations, persistence,
  paper service, safety boundary) and 10 SPY integration tests (dashboard vs Phases 2–12 engines,
  every page renders, no secret displayed).
- `docs/dashboard.md`, ADR-0022.

### Fixed
- `app.core.logging.configure_logging` now re-enables the uvicorn loggers: if they existed
  (Streamlit runs on uvicorn) when a `logging.config` call disabled existing loggers (Alembic's
  `fileConfig` in `migrations/env.py`), their records were silently dropped. Found by the full
  suite after adding the Streamlit page tests; regression test added. No other Phase 1–12 code
  changed.

### Changed
- `docs/paper-trading.md`: the paper broker's negative-cash safeguard is documented as an
  execution-simulation convention, not a change to the research backtester (Phase 8/11 frozen).

## [0.12.0] — 2026-10-07 — Phase 12: Paper Trading

Completes the Phase 12 WIP (`94480d0`) in a follow-up commit. Paper trading is a local
simulation; it does not transmit orders to any brokerage or market.

### Added
- `PaperTrader`: one per-session state machine (open: execute pending instruction; close:
  value, monitors, stop; decision: strategy → RiskManager → instruction; snapshot) shared by
  historical replay and incremental processing.
- Incremental, restart-safe accounts (`PaperStore`, CLI `run`/`status`): versioned, validated,
  deterministic `state.json` (atomic replace), append-only SHA-256 hash-chained `ledger.jsonl`,
  `manifest.json` provenance; idempotent per session; crash between ledger and state recovered
  only if re-processing reproduces the written events; corrupt/edited/incompatible files refused.
- Account accounting: average cost with capitalised costs; realized, unrealized and total P&L
  (= equity − initial) on fills, snapshots and metrics; snapshots reproducible from the ledger.
- Pending kinds (none/entry/exit/rebalance) and fill actions (entry/add/reduce/exit).
- 61 paper unit tests (incl. replay vs bar-by-bar with restarts, idempotency, crash recovery,
  tampering, data revision, exact rebalance P&L example, no-bypass of the risk layer, prefix and
  future-mutation tests including IDs) and 4 SPY integration tests (`test_paper_spy.py`).
- `docs/paper-trading.md`, ADR-0021.

### Fixed (Phase 12 WIP)
- Record IDs hashed a fingerprint of the whole dataset: appending or changing future bars
  renamed historical decisions/orders/fills. IDs are now point-in-time (account + type +
  session date); the dataset fingerprint is report metadata only.
- Phase 8 equivalence test compared metric dicts with `==` (NaN ≠ NaN) and paper metrics lacked
  realized/unrealized/total P&L; now NaN-aware with explicit tolerances and complete fields.
- A pending rebalance at the end of a replay was reported as neither pending nor executed.
- Negative cash residue (~−1.5e-11) inherited from the Phase 8 `buy_notional` rounding (exposure
  1 + 2e-16): the paper broker never books negative cash (shaves ≤ 1e-6 off such buys).
- Ruff E501 and formatting failures in the Phase 12 tests.

### Unchanged
- Phases 3–11 behaviour (Phase 11 refactor into `app/risk/manager.py` verified bit-identical to
  `13a462a` on SPY: 36/36 scenario × strategy runs). No database migration.

## [0.11.0] — 2026-10-07 — Phase 11: Risk Management

### Added
- `app/risk`: risk configuration and six pre-declared scenarios (control, fixed fraction 50%,
  volatility target 10%, drawdown lock 20%, ATR stop 3×, ATR risk 1%); sizing formulas;
  exposure caps (no leverage); drawdown and session-loss monitors with locks; close-based
  fixed-% and ATR stops with intraday-touch diagnostics and signal-reset re-entry; an [0, 1]
  exposure simulator reusing Phase 8 costs/metrics/execution validation (control == Phase 8
  exactly); requested vs approved decision records; diagnostics (exposure, interventions,
  states, stops, locks, costs, Phase 8 metrics); descriptive bootstrap comparisons vs control;
  CLI `python -m app.risk.cli`.
- 57 new tests (598 total): sizing/stop/monitor/limit units, OHLC ambiguity, stop timing,
  re-entry lockout, drawdown force-flat/block-entries, Phase 8 equivalence, signal
  immutability, no leverage/negative cash, prefix and future price/volume/indicator/state
  mutation leakage tests with a leaky-volatility control, determinism, SPY integration.
- `docs/risk-management.md`, ADR-0020.

### Fixed during development
- Fixed-fraction exposure drifted with price (rebalancing compared decided targets instead of
  actual exposure); the band now applies to the actual exposure at the open.

## [0.10.0] — 2026-10-07 — Phase 10: Machine Learning (research)

### Added
- `app/ml`: typed point-in-time feature dataset (86 features from Phases 3–6 with catalog,
  availability state, observed_at, fingerprint), close-to-close target with target timestamp,
  chronological split with purge, TRAIN-only preprocessing, logistic regression and random
  forest with fixed hyperparameters and seed, prediction → LONG/FLAT states, classification
  metrics + block-bootstrap AUC intervals, Phase 8 backtests across 5 windows × 4 cost
  scenarios vs all baselines and benchmarks, 12-test Holm/BH Sharpe family, pre-declared edge
  verdict, CSV/JSON experiment reports with git commit (+dirty flag), CLI `python -m app.ml.cli`.
- Dependency: scikit-learn.
- 39 new tests (541 total): feature timing (prefix/append, future OHLC/volume/adj_close and
  per-component mutation, pivot latency, leaky-feature control), target timing, split rejection
  of random/overlapping splits, TRAIN-only preprocessing (with leaky control), training
  isolation from validation/test features and targets, bit-identical determinism, next-open
  execution of ML states, report structure, edge rule, SPY integration.
- `docs/machine-learning.md`, ADR-0019.

### Result
- NO INCREMENTAL EDGE FOUND (both models; test AUC 0.519 / 0.505 with intervals including 0.5;
  no Sharpe difference survives Holm).

## [0.9.0] — 2026-10-07 — Phase 9: Statistical Validation

### Added
- `app/validation`: non-circular moving-block bootstrap (L 21, B 2,000, seed 20261007, 95%
  percentile intervals) with paired resampling; vectorised metrics identical to Phase 8;
  intervals for cumulative return, CAGR, volatility, Sharpe, Sortino, max drawdown
  (approximate), annualised mean return; pre-declared 30-test formal family with centred
  bootstrap p-values and Holm/BH corrections (declared m); descriptive comparisons (total-return
  benchmark, gross vs net, slices); predefined cost scenarios A/B/C/D; fixed-date slices;
  redundancy and concentration reports; typed models with full provenance; CLI
  `python -m app.validation.cli run` (optional CSV output).
- 44 new tests (502 total): bootstrap correctness (non-circular blocks, determinism, coverage
  sanity), Phase 8 metric equivalence, Holm/BH known values, declared-m handling, cost-scenario
  monotonicity, slice boundaries, append/future-mutation (OHLC, volume, adj_close, states)
  leakage tests, leaky-validator control, SPY integration with the default configuration.
- `docs/statistical-validation.md`, ADR-0018.

### Not changed
- Phase 4 zone expiry, bar-close timestamps, dividend treatment, all Phase 3–8 definitions.

## [0.8.0] — 2026-10-07 — Phase 8: Backtesting Engine

### Added
- `app/backtest`: event-driven single-instrument backtester — next-session-open execution with
  per-fill validation, all-in/all-out LONG/FLAT positions, explicit per-order costs
  (commission, half spread, slippage), gross vs net runs, equity curve, completed trades,
  marked-to-market open position, pending final decision, drawdown, metrics (cumulative, CAGR,
  volatility, Sharpe, Sortino, drawdown/duration, trade stats, exposure, turnover,
  concentration), price-return and total-return buy & hold benchmarks, temporal slicing
  (`start`/`end`/`period_label`), loader adding `adj_close`, CLI `python -m app.backtest.cli`.
- 42 new tests (458 total): exact accounting examples, cost/equity/P&L reconciliation,
  metric formulas and NaN cases, calendar execution timing, rejected look-ahead execution
  models, missing-session rejection, temporal slicing, prefix/full and future-mutation (incl.
  adj_close) leakage tests, future-dependent-rule control, full SPY integration test.
- `docs/backtesting.md` (incl. descriptive baseline diagnostic), ADR-0017.

### Fixed during development
- Empty trade tables had a different schema than non-empty ones; a fixed `TRADE_COLUMNS`
  schema is now always used.

### Not included (by design)
- No optimisation, statistical significance testing, shorting, leverage, broker/paper/live
  execution, persistence or migrations.

## [0.7.0] — 2026-10-07 — Phase 7: Baseline Strategies

### Added
- `app/strategies`: `Strategy` contract (declared features, LONG/FLAT/INSUFFICIENT_DATA),
  six fixed baselines — buy & hold (benchmark), SMA200 trend, SMA20/50 crossover, RSI14 > 50,
  price-action uptrend, regime `trending_up` — and `StrategyEngine` (on-demand features from
  existing engines, declared-column isolation, calendar-based `observed_at`/`effective_at`,
  `state_in_effect`, reasons, summary, combined fingerprint); CLI `python -m app.strategies.cli`.
- 45 new tests (416 total): exact rules and boundaries, NaN ≠ FLAT, feature isolation,
  calendar timestamps (holiday, weekend, early close), execution latency, Price Action
  confirmation latency, prefix/full and future-mutation leakage tests with a leaky-rule
  control, SPY integration regression snapshot of state counts.
- `docs/baseline-strategies.md`, ADR-0016.

### Not included (by design)
- No returns, performance, backtesting, costs, fills, orders, sizing, optimisation, ML,
  shorting, persistence or migrations.

## [0.6.0] — 2026-10-07 — Phase 6: Market Regimes

### Added
- `app/regimes`: `RegimeEngine` composing the Price Action and Indicator engines into
  descriptive regime dimensions — trend, volatility (causal mid-rank percentile of realized
  volatility vs the previous 756 bars, ≥ 252 references; low/normal/high/extreme at
  0.20/0.80/0.95), momentum (unanimous RSI/ROC/MACD; extremes at RSI 70/30), instrument-level
  volume participation — plus an explicit trend × volatility composite, per-dimension
  `changed`/`age`, combined config fingerprint, `state_as_of`, CLI `python -m app.regimes.cli`.
- 88 new tests (371 total): exact percentile/threshold/composite tests, upstream-integration
  checks, Price Action volume-invariance proof, warm-up boundaries, edge cases, prefix and
  future-mutation leakage tests with a full-sample-percentile control, SPY integration
  regression snapshot.
- `docs/market-regimes.md`, ADR-0015.

### Not included (by design)
- No breadth (no point-in-time constituents), macro/news regimes, signals, strategies,
  backtests, ML, risk or broker code; no persistence, no migrations.

## [0.5.0] — 2026-10-07 — Phase 5: Technical Indicators

### Added
- `app/indicators`: `IndicatorEngine` with SMA/EMA (20, 50, 200), MACD 12/26/9, Wilder RSI 14,
  Stochastic 14/3/3, ROC 12, true range, Wilder ATR 14, Bollinger 20/2 (ddof 0), realized
  volatility 20 (log returns, ddof 1, √252), relative volume 20 (current bar excluded), OBV;
  `IndicatorConfig` with validation and fingerprint; per-column catalog with exact warm-up;
  `value_as_of`; CLI `python -m app.indicators.cli latest`.
- `scripts/crossvalidate_indicators.py`: cross-check against TA-Lib 0.8.1 (ephemeral); all
  indicators match or differ only by verified, documented conventions (MACD initialisation,
  %K publication start, OBV starting offset).
- 56 new tests (283 total): hand-computed examples, independent loop reference, warm-up
  boundaries for every column, edge cases (flat prices, zero/missing volume, zero
  denominators, scale ×1e±6), prefix/full and future-mutation leakage tests with a centred-
  window control, SPY regression snapshot, full-history SPY integration test.
- `docs/technical-indicators.md`, ADR-0014.

### Not included (by design)
- No persistence, signals, strategies, regimes, backtests, ML or broker code.

## [0.4.0] — 2026-10-07 — Phase 4: Price Action Engine

### Added
- `app/price_action`: `PriceActionEngine` with pivot detection and explicit confirmation
  latency (`pivot_ts` vs `confirmed_at`), HH/HL/LH/LL/EH/EL labels, structure classification
  (uptrend/downtrend/range/transition/insufficient_data), trend + structural quality + evidence,
  incremental support/resistance zones (tolerance, width cap, merging, availability, touches),
  close-based breakout/breakdown, retest, rejection, objective sweep events, separately
  timestamped outcomes (failed/held/pending), consolidation episodes, range
  expansion/contraction, `state_as_of` for future multi-timeframe alignment.
- CLI `python -m app.price_action.cli scan` (descriptive counts).
- 78 new tests (227 total), incl. truncation and future-perturbation leakage tests with a
  leaky-confirmation control, boundary tests for every threshold, SPY 2020 swing snapshot.
- `docs/price-action-engine.md`, ADR-0012, ADR-0013.

### Not included (by design)
- No persistence, signals, decisions, indicators, strategies, backtests, ML or broker code.

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
