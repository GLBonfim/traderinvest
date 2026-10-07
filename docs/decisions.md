# Architecture Decision Records

Each decision records context, the choice, and its consequences. Decisions are not edited after
acceptance; they are superseded by a new ADR.

---

## ADR-0001 — Python 3.12 managed with uv

- **Status:** Accepted (2026-10-07, owner approval)
- **Context:** Python 3.12 and 3.14 are installed. Scientific/ML libraries (LightGBM, statsmodels,
  some calendar libraries) lag behind new Python releases.
- **Decision:** Pin Python 3.12 (`.python-version`, `requires-python = ">=3.12,<3.13"`). Use `uv`
  for environments and a committed `uv.lock` for reproducible installs.
- **Consequences:** Reproducible dependency sets. Upgrading Python is a deliberate, tested change.

## ADR-0002 — PostgreSQL 16 in Docker Compose, app on the host

- **Status:** Accepted (2026-10-07)
- **Context:** No local PostgreSQL. Docker Desktop available. Research data should stay local.
- **Decision:** `postgres:16-alpine` via `docker-compose.yml`, bound to `127.0.0.1` only, named
  volume, `TZ=UTC`. The application runs on the host with `uv run`.
- **Consequences:** Fast dev loop. A containerised app image is deferred until needed (e.g. for
  scheduled ingestion or deployment). TimescaleDB may be evaluated if bar volume requires it.

## ADR-0003 — SQLAlchemy 2 (sync, psycopg 3) + Alembic

- **Status:** Accepted (2026-10-07)
- **Decision:** Synchronous SQLAlchemy 2.x ORM/Core with psycopg 3. Alembic for every schema
  change; DB URL built from env in `migrations/env.py`. Deterministic constraint naming.
  `alembic check` runs in the integration tests.
- **Consequences:** Research workloads are batch/CPU-bound; sync is simpler and adequate. FastAPI
  runs sync endpoints in a thread pool. Async can be introduced later if the API needs it.

## ADR-0004 — yfinance for prototyping only; provider abstraction from the start

- **Status:** Accepted (2026-10-07). Implementation: Phase 2.
- **Context:** No paid data plans in this phase. yfinance is unofficial, may break, has ToS
  restrictions and only ~730 days of 1h history.
- **Decision:** Phase 2 starts with **SPY daily bars from yfinance**. All access goes through a
  `DataProvider` interface; strategies never call a provider directly. Massive (Polygon) is the
  intended future primary provider and must be pluggable without touching strategy code.
  Every bar records its `provider`.
- **Consequences:** Results produced from yfinance data are labelled as prototype-grade. Intraday
  walk-forward research is limited until a better provider is integrated.

## ADR-0005 — 4H bars: session-anchored aggregation of 1H regular-session bars

- **Status:** Accepted (2026-10-07). Implementation: Phase 2/3.
- **Context:** The US regular session is 09:30–16:00 America/New_York (6.5 h). A 4H grid that
  ignores the session (e.g. UTC-aligned) produces bars mixing pre-market, regular and after-hours
  trading, and shifts with daylight-saving changes.
- **Decision:**
  1. Source: 1H bars of the **regular session only** (extended hours excluded).
  2. 1H bars are anchored to the session open: 09:30, 10:30, 11:30, 12:30, 13:30, 14:30, 15:30
     (the last one is 30 min).
  3. 4H bars are anchored to the session open of each trading day, in exchange-local time:
     - **Bar A:** 09:30–13:30 (four 1H bars),
     - **Bar B:** 13:30–16:00 (2.5 h: 13:30, 14:30, 15:30 bars).
     4H bars **never span two sessions** (no overnight bars).
  4. Early-close days (e.g. 13:00 close) come from the exchange calendar: Bar A is truncated at
     the close and Bar B does not exist that day.
  5. Aggregation: open = first open, high = max high, low = min low, close = last close,
     volume = sum.
  6. A 4H bar is `is_closed = true` only when **all** constituent 1H bars are closed and the
     session segment has ended. A bar is never built from an incomplete 1H bar.
  7. Missing constituent 1H bars → the 4H bar is not emitted silently; a `data_quality_events`
     row is recorded and the bar is flagged/withheld per validation rules.
  8. Timestamps are converted to UTC for storage; `ts` is the 4H bar open.
- **Consequences:** Bar B is shorter than Bar A, so per-bar statistics (range, volume, ATR) differ
  structurally between the two. Analyses must account for this (e.g. normalise by duration or
  include a `segment` feature). This methodology must be documented in every result that uses 4H.

## ADR-0006 — Real-money execution disabled and blocked by default

- **Status:** Accepted (2026-10-07)
- **Decision:**
  - `TradingMode` enum contains only `disabled` (default) and `paper`. Any other value, including
    `live`, fails settings validation at startup.
  - `app.core.safety.refuse_real_money_order()` always raises `RealMoneyExecutionBlockedError`;
    any future real-venue code path must call it.
  - Startup re-checks the mode (`assert_safe_trading_mode`) and logs `real_money_execution=blocked`.
  - The API exposes no order/trade/broker endpoints; a unit test asserts the exact route set.
- **Consequences:** Enabling real-money execution requires code changes, a new ADR superseding this
  one, completion of all validation phases, and explicit owner approval.

## ADR-0007 — Streamlit as the initial dashboard

- **Status:** Accepted (2026-10-07). Implementation: Phase 13.
- **Decision:** Streamlit for the first dashboard (fast iteration for research views). It reads
  through the API/DB layer, never directly from providers. Not added as a dependency until used.
- **Consequences:** A React/Next frontend may replace it later if requirements outgrow Streamlit.

## ADR-0009 — Daily bars stamped with session open (UTC); data-quality policy

- **Status:** Accepted (2026-10-07, Phase 2)
- **Decision:**
  - Daily `price_bars.ts` = XNYS session open in UTC, mapped from the provider's exchange-local
    date. `is_closed` = session close <= `as_of`.
  - Structurally invalid bars are **excluded** and recorded; plausible-but-unusual bars
    (outliers, zero volume, splits) are **kept and flagged**. Outliers are never removed.
  - Changes to stored bars are applied but recorded: raw OHLCV revisions of closed bars as
    `bar_revised` warnings; adj_close restatements as one info event per run.
  - adj_close differences <= 1e-5 relative are treated as provider float noise: the stored value is
    kept and the count is logged (`adj_close_noise_ignored`). Evidence: consecutive Yahoo requests
    differed by up to 1.4e-6 relative on ~7,000 SPY bars; a real dividend restatement is ~1e-3.
  - Every ingestion is an `ingestion_runs` row; events reference their run.
- **Consequences:** Re-runs are idempotent. Daily `ts` is comparable with future intraday bars
  (same session-open anchor). The noise tolerance must be revisited per provider.

## ADR-0010 — Candlestick observations computed on demand (no persistence yet)

- **Status:** Accepted (2026-10-07, Phase 3)
- **Context:** Observations are a pure, deterministic function of (closed bars, `CandleConfig`,
  `ENGINE_VERSION`). The full SPY history takes ~130 ms. No component consumes stored
  observations yet; the pattern database with forward returns (outcomes) belongs to the
  statistical-validation phase.
- **Decision:** No tables in Phase 3. The engine is called on demand. Every analysis carries
  `engine_version` and `config_fingerprint` so that a later persisted feature set can record
  exact provenance (bars + provider + engine version + thresholds) in its own versioned
  table, separate from raw `price_bars`.
- **Consequences:** No derived data can drift from its definition. Persistence will be
  introduced when a consumer (validation/backtests) needs it, as a versioned derived-feature
  table — never mixed into raw market data.

## ADR-0011 — Candlestick definitions: raw OHLC, explicit thresholds, past-only context

- **Status:** Accepted (2026-10-07, Phase 3)
- **Decision:**
  - Geometry uses provider raw OHLC; `adj_close` is not mixed into candle shapes.
  - Every detector uses explicit thresholds from `CandleConfig` (docs/candlestick-engine.md).
  - Prior trend = volatility-normalised net move over 10 bars ending before the pattern's first
    candle. Hammer/hanging man and inverted hammer/shooting star are distinguished by it;
    for other reversal patterns it is reported as `context_requirements_met`, not used to
    suppress the observation, so its value can be tested later.
  - `orientation` is the conventional label only; `strength` is geometric quality only.
  - Ratios are rounded to 1e-10 for threshold stability.
- **Consequences:** Definitions are reproducible and testable; sensitivity to thresholds and to
  the trend definition must be examined in validation phases before any conclusion.

## ADR-0012 — Price Action Engine: sequential point-in-time processing, no persistence

- **Status:** Accepted (2026-10-07, Phase 4)
- **Decision:**
  - Pivots need future bars by definition; each carries `pivot_ts` and `confirmed_at`
    (`= pivot + swing_right_bars`), and every consumer uses `confirmed_at`.
  - Bars are processed sequentially: events at t use zones available at t−1; pivots confirmed
    at t are added after; the state row for t is emitted last. Nothing reads bars after t.
  - Output columns have explicit dtypes so that the representation of state(T) cannot depend on
    later rows (found during Phase 4 testing: all-None columns changed type with more data).
  - Observations are computed on demand like candlestick observations (same reasoning as
    ADR-0010); `engine_version` + `config_fingerprint` provide provenance.
  - Candle geometry (wick ratios, direction) is reused from `app.candles`; the engines remain
    separate and are composed only by consumers.
- **Consequences:** Point-in-time correctness holds by construction and is tested (truncation,
  future perturbation, leaky-confirmation control). Sequential processing costs ~1.4 s for the
  full SPY history, acceptable for research.

## ADR-0013 — Observation vs outcome; objective sweep event

- **Status:** Accepted (2026-10-07, Phase 4)
- **Decision:**
  - Breakout/breakdown outcomes (failed / held / pending) are stored in a separate `outcomes`
    table with `outcome_at`; event rows never contain outcomes and are never rewritten.
  - A "liquidity sweep" is implemented only as an objective OHLCV event named `sweep`: trade
    beyond a confirmed zone and close back on the original side. No claim about orders,
    stops or institutional liquidity is made, since OHLCV cannot support it.
  - Breakouts are close-based; wick-only excursions are sweeps (or rejections), never breakouts.
- **Consequences:** Backtests can use an outcome only from its `outcome_at`, preventing the
  classic "false breakout" look-ahead.

## ADR-0008 — Local Git now, private GitHub remote later

- **Status:** Accepted (2026-10-07)
- **Decision:** Local repository on `main`. `.gitignore` excludes `.env`, secrets and all data
  formats; `.env.example` documents configuration. The private GitHub remote is added by the owner
  (instructions in README). CI is deferred.
