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

## ADR-0014 — Technical indicator conventions

- **Status:** Accepted (2026-10-07, Phase 5)
- **Decision:**
  - Indicators are small, transparent pandas/numpy implementations; no TA library dependency.
    TA-Lib is used only as an external cross-check (`scripts/crossvalidate_indicators.py`, run in
    an ephemeral environment).
  - Raw OHLC and raw volume for every indicator (consistent with the other engines); no
    adjusted close and no corporate-action handling in this phase.
  - Recursive smoothers (EMA α = 2/(n+1); Wilder α = 1/n for RSI and ATR) are seeded with the
    SMA of their first n valid inputs; MACD uses standalone EMAs (differs from TA-Lib's MACD
    only during initialisation; verified < 1e−12 after 300 bars).
  - A value is published as soon as it is computable (e.g. %K before %D), NaN before.
  - Bollinger std ddof = 0; realized volatility: log returns, ddof = 1, √252; relative volume
    excludes the current bar; OBV starts at 0; undefined values are NaN.
  - Computed on demand, not persisted (same reasoning as ADR-0010).
- **Consequences:** Every value is reproducible from (bars, `IndicatorConfig`,
  `ENGINE_VERSION`). Return-based indicators include ex-dividend drops; switching to adjusted
  closes would be a new engine version.

## ADR-0015 — Market regime conventions

- **Status:** Accepted (2026-10-07, Phase 6)
- **Decision:**
  - Regimes are separate dimensions composed from existing engines: trend = Price Action
    structure; volatility = causal percentile of realized volatility; momentum = unanimous
    RSI/ROC/MACD agreement; participation = instrument-level relative volume (explicitly not
    breadth). No formula is re-implemented.
  - Causal percentile: previous 756 bars (T excluded), ≥ 252 valid references, mid-rank ties,
    no interpolation, values rounded to 1e−10 before ranking. Cut-offs 0.20 / 0.80 / 0.95.
  - Composite = explicit trend × volatility table; momentum and participation stay separate.
  - Raw labels per bar; no smoothing/hysteresis; `changed` and `age` are causal observations.
  - Missing volume is passed to the Price Action Engine as 0 because none of its outputs depend
    on volume (tested); this avoids changing Price Action behaviour.
  - Thresholds chosen for clarity (quantiles, textbook RSI levels), never from returns or
    trading outcomes. Computed on demand, not persisted.
- **Consequences:** Every label is reproducible from (bars, three configs, engine versions); the
  combined `config_fingerprint` covers all three. Volatility labels are relative to the trailing
  history and unavailable for the first 272 daily bars.

## ADR-0016 — Baseline strategy contract and timing

- **Status:** Accepted (2026-10-07, Phase 7)
- **Decision:**
  - A strategy declares its feature columns; the engine passes ONLY those columns to its rule
    (isolation by construction). Six fixed baselines, LONG/FLAT only, no scores, no
    combinations, no candlestick rules, no shorting.
  - Three states: LONG, FLAT, INSUFFICIENT_DATA. Missing inputs never become FLAT.
  - Timing: `observed_at` = session close of T, `effective_at` = next session open, both from
    the XNYS calendar (known in advance, prefix-safe); `state_in_effect(T)` = state(T−1).
    No execution price is attached.
  - Parameters are fixed textbook conventions (SMA200, SMA20/50, RSI14 vs 50); no optimisation.
  - States computed on demand; signal/trade persistence will be designed with backtesting and
    experiment tracking (Phase 8+).
- **Consequences:** Phase 8 receives unambiguous, point-in-time states and must make every
  execution assumption (fill price, costs) explicit itself.

## ADR-0017 — Backtest execution and cost conventions

- **Status:** Accepted (2026-10-07, Phase 8)
- **Decision:**
  - Single execution model `next_session_open`: a state decided at the close of T executes at
    the raw open of the next XNYS session (bar T+1). The engine validates every fill (after the
    decision, at the next session, equal to `effective_at`, at the open price).
  - Final-bar decisions are reported as pending, never executed on an invented bar; open
    positions are marked to market at the last close, separate from completed trades.
  - Position ∈ {0, 1}, all-in/all-out, fractional shares, no shorting/leverage/margin.
  - `INSUFFICIENT_DATA` keeps the previous target (0 before the first valid state).
  - Costs per order: commission max(1.00 + N·bps, minimum), half of a 2 bps quoted spread,
    2 bps slippage — generic research assumptions, never calibrated on results; gross and net
    are produced from two runs on identical fills.
  - Raw prices for execution and strategy P&L; adjusted prices only for a separate
    total-return buy & hold benchmark.
  - Metrics: A = 252, rf = 0, std ddof = 1, Sortino downside over all periods with target 0,
    drawdown peak includes initial capital; undefined metrics are NaN.
  - Results computed on demand; persistence will come with experiment tracking.
- **Consequences:** Results are reproducible from (bars, strategy fingerprint, backtest
  fingerprint). Timing strategies earn price return only; comparisons must use the
  price-return benchmark or acknowledge the dividend gap.

## ADR-0018 — Statistical validation methodology

- **Status:** Accepted (2026-10-07, Phase 9)
- **Decision (all fixed before results were seen):**
  - Non-circular moving-block bootstrap, L = 21 sessions, B = 2,000, percentile 95% intervals,
    seed 20261007; identical index matrix for all series of a slice (paired comparisons);
    resampling never crosses a slice boundary.
  - Formal family = 30 pre-declared tests (5 timing strategies vs the price benchmark + 10
    pairs among them; annualised mean-return and Sharpe differences) on the full slice with the
    default costs; centred bootstrap p-values; Holm (primary, any dependence) and BH
    (secondary); undefined tests count in m. Everything else is descriptive (no p-values).
  - Predefined cost scenarios A 0 bps, B 5 bps, C 10 bps (all-inclusive per order, 0/order)
    and D = Phase 8 default; robustness, not optimisation.
  - Fixed-date slices `full`, `early` (≤ 2009-12-31), `late` (≥ 2010-01-01); features computed on
    bars ≤ slice end; not train/validation/test sets.
  - Phase 7/8 methodology unchanged (raw-price strategy P&L, total-return benchmark only on
    adjusted prices); outputs ephemeral (optional CSV to git-ignored `data/`), no migration.
- **Consequences:** Results are reproducible from (bars, config fingerprint). They quantify
  uncertainty; they are not evidence of economic value or future profitability.

## ADR-0019 — Machine-learning research protocol

- **Status:** Accepted (2026-10-07, Phase 10)
- **Decision (fixed before results):**
  - Target: raw close(T+1) > close(T); features strictly ≤ close(T); last row unscored.
  - Chronological split TRAIN ≤ 2009-12-31, VALIDATION 2010–2017, TEST ≥ 2018 with a
    1-session purge; random splits rejected by construction and by tests.
  - 86 features from the existing engines; ML-specific scale-free representations for
    price-level series; Price Action outcomes and adjusted close excluded.
  - Preprocessing (missing indicators, TRAIN medians, standardisation) fitted on TRAIN only.
  - Two model families (logistic regression, shallow random forest), fixed hyperparameters,
    seed 20261007, no search, no class weights, threshold 0.5; models trained once on TRAIN.
  - Evaluation: classification metrics + Phase 8 backtests (next-session open, raw prices,
    Phase 8 costs, Phase 9 scenarios) + Phase 9 paired block bootstrap; 12-test Sharpe family
    with Holm; pre-declared edge rule (AUC CI > 0.5, Holm-significant Sharpe gain vs all six
    references, benchmark beaten at 10 bps).
  - Outputs ephemeral (git-ignored `data/ml/`); scikit-learn added as a dependency.
- **Consequences:** Result on SPY: NO INCREMENTAL EDGE FOUND for both models. Any future model
  must beat this protocol, not a re-tuned version of it.

## ADR-0020 — Risk overlay architecture and conventions

- **Status:** Accepted (2026-10-07, Phase 11)
- **Decision:**
  - The risk layer sits between strategy states and execution and never modifies the signal;
    each decision stores requested, sized, approved and target exposure with reasons.
  - Exposures in [0, 1] are simulated in `app/risk` by generalising the Phase 8 accounting
    (same costs, metrics and next-session-open validation) instead of modifying Phase 8; the
    no-overlay control reproduces Phase 8 exactly (tested on all baselines).
  - Rebalancing only when |actual exposure − target| ≥ 0.10 at the open, or on entry/exit.
  - Sizing: full, fixed fraction, volatility target (σ*/σ̂20), ATR risk (b·close/(k·ATR14)),
    capped at 1; undefined inputs → exposure 0.
  - Stops evaluated on the close only (intraday ordering never inferred; touches reported;
    intraday stop orders unsupported); re-entry after a stop requires a signal reset.
  - Drawdown lock (force_flat / block_entries) with cooldown and HWM reset; session-loss and
    consecutive-loss locks as disabled infrastructure.
  - Six pre-declared scenarios with the control first; overlay-vs-control comparisons are
    descriptive bootstrap intervals only (360), no selection.
- **Consequences:** The effect of each control is measurable against an identical control.
  Results show the expected return/risk trade-off; locks and stops are path-dependent.

## ADR-0021 — Paper trading: one state machine, durable file ledger, point-in-time IDs

- **Status:** Accepted (2026-10-07, Phase 12)
- **Context:** The Phase 12 WIP (`94480d0`) offered historical replay only, named every record
  with a hash of the whole dataset (appending a bar renamed history), did not report pending
  rebalances and had no persistent state.
- **Decision:**
  - Paper trading is a **local simulation**; it does not transmit orders to any brokerage or
    market. `TradingMode` stays {disabled, paper}; `refuse_real_money_order()` stays
    unconditional; `app/paper` has no network/broker imports (tested).
  - One per-session state machine (`PaperTrader.process`) drives both historical replay and
    incremental processing, so they are equivalent by construction (and tested to be).
  - Flow: strategy state → RiskManager → risk-approved `Instruction` → `PaperBroker`; the broker
    accepts nothing else. Execution at the next XNYS open with Phase 8 costs via the Phase 11
    `plan_order`/`apply_order`.
  - Record IDs from creation-time information only (account = config + strategy + instrument +
    timeframe; record = account + type + session date; ledger event = account + sequence).
    Dataset fingerprints are report metadata only.
  - Durable state: per account, `state.json` (versioned, validated, deterministic, atomic
    replace) + `ledger.jsonl` (append-only, SHA-256 hash chain) + `manifest.json`, under the
    git-ignored `data/paper/accounts/`. No database migration: the ledger is append-only, per
    account and never queried relationally.
  - Commit = ledger append (fsync) then atomic state write; an interrupted commit is completed
    only if re-processing reproduces the written events exactly. Corrupt, edited, truncated or
    incompatible files raise; nothing is ever repaired or rewritten.
  - Accounting: average cost with costs capitalised; total P&L = realized + unrealized = equity
    − initial. Cash may never be negative: the broker shaves ≤ `cash_tolerance` (1e-6) off a
    buy to absorb the Phase 8 `buy_notional` rounding residue (~1e-11), the only deviation
    from Phase 8/11 arithmetic.
  - Pending kinds none/entry/exit/rebalance; fill actions entry/add/reduce/exit.
- **Consequences:** Restart and idempotency are testable properties (same bar twice, restart,
  crash between ledger and state). Paper equals Phase 8/11 within 1e-6 currency (not bit-exact).
  Concurrency (several writers per account) and a real broker remain out of scope.

## ADR-0022 — Research dashboard: Streamlit over pure services, read-only by default

- **Status:** Accepted (2026-10-07, Phase 13)
- **Decision:**
  - Streamlit + Plotly for the first dashboard (no React/Next.js yet). Three layers: domain
    engines → `app/dashboard/services` (pure, deterministic, no Streamlit/Plotly) → thin pages.
    No financial calculation lives in the UI; integration tests compare every displayed number
    with its engine on the full SPY history.
  - The dashboard is a *daily research system*: it reads the local database and local files,
    never fetches market data, binds to 127.0.0.1, disables usage statistics, and checks the
    trading mode at start-up. No LLM is used; the session summary is template-based and lists
    its source fields.
  - Historical views are point-in-time (pivots after `confirmed_at`, events after
    `available_at`, zones from the session's state).
  - Caching keyed by dataset identity (app version, bars, first/last bar, last ingestion) and a
    configuration key (all engine/backtest/validation/ML/risk fingerprints). Paper accounts are
    never cached. Phase 9/10 results are persisted under `data/dashboard/research/` and recomputed
    only by an explicit, labelled action (never on page load; models are never retrained
    automatically).
  - The only mutating controls are two PAPER SIMULATION actions delegated to `app.paper`
    (create account, process available sessions). No broker, credentials, order endpoint or
    live mode; `TradingMode` stays {disabled, paper}.
- **Consequences:** The UI can be replaced later (e.g. a web front-end) by reusing the services.
  Streamlit reruns make full-history charts take seconds; acceptable for daily research use.

## ADR-0023 — Alerts: downstream-only, transition rules, point-in-time IDs, file store

- **Status:** Accepted (2026-10-08, Phase 14)
- **Decision:**
  - Alerts are downstream consumers of recorded domain outputs (engines, risk decisions, paper
    ledgers, ingestion runs, data-quality events): domain event → rule → alert → channel. They
    never create decisions, instructions or orders; `app/alerts` imports no trading module.
  - Explicit deterministic rules that fire on transitions (wall-clock conditions as episodes);
    three severities (INFO, WARNING, CRITICAL) describing operational attention only; fixed
    message templates; no LLM.
  - Identity: `alert_id = sha256(event_type | instrument | strategy/account | session or
    episode | state)[:16]` — no dataset fingerprint, version or run time (Phase 12 discipline).
  - Persistence: git-ignored `data/alerts/` with hash-chained append-only `alerts.jsonl` and
    `deliveries.jsonl` plus an atomic, versioned `state.json`; no database migration (alerts
    are append-only, local and never queried relationally).
  - Channels: console and dashboard always; one optional generic JSON webhook configured only
    through `ALERT_WEBHOOK_URL` (no vendor SDKs). Bounded retries (3 attempts, 60 s / 300 s
    backoff, permanent failure), non-blocking, failures recorded and isolated, URL never logged.
  - Reporting starts at a store-level `since` (default: latest stored session).
- **Consequences:** Re-processing, refreshes and restarts never duplicate alerts or deliveries
  (external delivery is at-least-once only across a crash between send and record). Monitoring
  of `refuse_real_money_order()` invocations would require changing the safety module and is
  left for an owner decision.

## ADR-0024 — Daily operations: orchestration-only pipeline, file lock, local scheduler

- **Status:** Accepted (2026-10-08, Phase 14.5)
- **Decision:**
  - `app/operations` orchestrates existing subsystems in a fixed order — PRECHECK, then per
    completed XNYS session INGEST → VALIDATE → PAPER → ALERTS, then HEALTH → COMPLETE — through a
    `Services` port (real adapter: ingestion, stored-bar checks, Phase 12 paper store, Phase 14
    alert run, health). It contains no financial logic and adds no trading capability.
  - Sessions are eligible after the official close (calendar, early closes included) + 30 min
    grace; incomplete sessions are never processed; catch-up is session by session, oldest
    first, at most 10 per run.
  - Durability and recovery rely on the subsystems' idempotency; the operations checkpoint
    (last completed session) only narrows the pending set. History is an append-only,
    hash-chained `runs.jsonl` plus an atomic, read-modify-write `state.json` in git-ignored
    `data/operations/`. No database migration.
  - Single-run lock: atomic exclusive file with owner metadata; broken automatically only for a
    verified-dead process on the same host; otherwise an explicit `unlock --force`.
  - Failure policy: ingestion/validation/paper-stage failures stop later sessions; paper account
    failures are isolated; alert and health failures never roll anything back; late data uses
    bounded retries (3 attempts, at least 30 min apart) and is never fabricated.
  - Scheduler: one long-running local process with injected clock/sleep, polling every 5 min and
    running only when due. Rejected: installing OS scheduler entries from code, cron,
    APScheduler, Celery/Redis, Windows services (more moving parts, harder to test, no benefit
    for one daily job on one machine).
  - Version 0.14.5: SemVer 0.x allows any change in any release; the number keeps phase and
    version aligned, and broker integration remains Phase 15.
- **Consequences:** Daily operation is one command or one long-running process; restarts and
  duplicate wake-ups are safe. Running unattended still requires the user to start the scheduler.

## ADR-0025 — Broker sandbox: Alpaca paper only, fixed endpoint, mirror of approved decisions

- **Status:** Accepted (2026-10-09, Phase 15)
- **Decision:**
  - Vendor: Alpaca, PAPER Trading API only, through its official REST API with the standard
    library (no vendor SDK), behind the project's `BrokerGateway` abstraction.
  - Endpoint: the constant `https://paper-api.alpaca.markets`. No parameter, setting or
    environment variable selects an endpoint; `https://api.alpaca.markets` is rejected
    explicitly, every other host/scheme/port is refused, redirects are never followed. No live
    credentials, no live-mode documentation, no switching mechanism.
  - Orders are derived only from RiskManager-approved decisions of linked local paper accounts
    (no manual-order surface); the local PaperBroker remains the execution simulation.
  - Default state: unarmed, nothing linked, no credentials; submission additionally requires
    `TRADING_MODE=paper` and a released kill switch; caps on notional and daily orders.
  - Idempotency: deterministic client_order_id, write-ahead log, lookup before every POST and
    after ambiguous failures; hash-chained `data/broker/orders.jsonl`.
  - Default order policy: market `opg` whole shares (opening auction ≈ next-session-open
    convention); fractional `day` orders available but not default.
- **Consequences:** No external order has been placed; the first sandbox order needs explicit
  owner approval. Sandbox fills will differ from the local simulation (auction price, whole
  shares, vendor-simulated partial fills); reconciliation reports differences without acting.

## ADR-0026 — Broker sandbox: capital policy and OPG submission window

- **Status:** Accepted (2026-10-10, Phase 15 validation; amends ADR-0025, which stays in force)
- **Context:** The pre-submission review found that (1) the 10,000 sandbox capital only sized
  the order at the decision close: a market `opg` order has no maximum price, so its cost was
  unbounded; (2) the OPG cut-off was not enforced — an order sent between 09:28 and 09:30 ET
  (or any time in Alpaca's 09:28–19:00 ET rejection band) would be rejected by Alpaca.
- **Decision:**
  - Three separate limits: sandbox budget (`allocated_capital`, cost of the resulting position),
    risk limits (`max_order_notional`, `max_orders_per_day`, per order/day), and the Alpaca
    account (ACTIVE, not blocked, estimated cost ≤ min(cash, buying power), no margin), each
    checked independently; the account is never used to size anything.
  - `capital_policy` (default `block_unbounded`): a buy whose fill price cannot be bounded — every
    market order — is blocked (`capital_bound_unenforceable`). Sells are never held back by
    capital checks. `reference_price_unguaranteed` (budget checked at the decision close only)
    is never the default and is documented as not a guarantee. Reconciliation reports filled
    buys whose actual cost exceeded the budget.
  - OPG window (`app/broker/timing.py`, pure, injectable clock): send only at or after 19:00 ET
    of the previous XNYS session and before 09:28 ET of the auction session, never inside the
    daily 09:28–19:00 ET band (applied on every calendar day because the vendor documentation
    is silent about weekends/holidays), each boundary tightened by `opg_safety_seconds` (60 s);
    America/New_York via zoneinfo (DST), sessions from the XNYS calendar (holidays). Checked
    with the gates and again immediately before the POST.
- **Consequences:** With the defaults no market buy is sent; the first sandbox entry is blocked
  until the owner chooses between (a) a price-bounded limit-on-open order (limit = budget /
  quantity; may not fill), (b) accepting an unbounded market cost
  (`reference_price_unguaranteed`), or (c) keeping entries blocked. Some times Alpaca would
  accept (e.g. a weekend afternoon) are skipped by the conservative band.

## ADR-0027 — Broker sandbox: limit-on-open buys bounded by the budget

- **Status:** Accepted (2026-10-10, owner choice (a) of ADR-0026)
- **Context:** A market-on-open buy cannot be kept within the 10,000 sandbox budget. Alpaca's
  documentation (2026-10-10) supports `opg` with type `limit` as "limit on open" (LOO) for
  equities, whole shares only, `limit_price` at most 2 decimals (≥ 1.00) or 4 (< 1.00). The
  order-type table marks OPG with "contact the sales team", without saying whether this applies
  to Trading API / paper accounts.
- **Decision:**
  - New default order policy `opg_whole_shares_loo_buys`: buys are limit-on-open, sells stay
    market-on-open (an exit is never held back by a price limit).
  - Effective budget = `allocated_capital − order_cost_allowance` (10,000 − 1.00; Alpaca charges
    no commission on equity buys, the allowance mirrors the project's flat research commission).
    Target quantity is sized on the effective budget at the decision close (whole shares).
  - `limit = floor_to_increment(effective budget / (held + bought))` in `Decimal`
    (`app/broker/pricing.py`): the highest valid price with (held + bought) × limit ≤ effective
    budget; ≥ the decision close by construction. The notional cap and the account cash check use
    the limit (worst case). The adapter refuses locally limit orders without a price, with
    fractional shares or off the increment, and market orders with a price.
  - The OPG window (ADR-0026) applies unchanged and is re-checked before the POST.
  - Reconciliation adds `unfilled` (final state without any fill) and `limit_violations` (a buy
    filled above its limit, which should be impossible); partial fills and budget breaches as
    before. Nothing is corrected or retried automatically.
- **Consequences:** The resulting position's value at the fill price is ≤ the effective budget
  and, with the allowance, ≤ 10,000 — a guarantee resting on the vendor honouring the limit. An
  entry may not fill (auction above the limit); the sandbox then diverges from the local
  simulation, which always fills at the open. The sandbox sizes one share less than before in
  some cases (sizing on 9,999). Vendor acceptance of LOO for this account is unverified until
  the first submission.

## ADR-0028 — Read-only market API for the web UI

- **Status:** Accepted (2026-10-11, owner approval; frontend phase 3B)
- **Context:** The static web UI at `/ui` (same origin, CSP `'self'`) needs stored market
  analysis for its Market Analysis page. Until now the API exposed only `GET /health`, and
  `tests/unit/test_api.py` pinned that route set.
- **Decision:**
  - Exactly two new routes, both `GET`, under `/api/v1/market`: `/summary` (latest closed
    session: bar, freshness, regimes, indicators with warm-up status, candle observations,
    price-action structure) and `/series?range=3m|6m|1y|2y|5y` (default `1y`; 63/126/252/504/1,260
    sessions; closed OHLCV plus SMA 20/50/200, Bollinger, RSI 14 and MACD). SPY only; no symbol,
    `as_of` or any other parameter (unknown parameters → 422). The route whitelists in the unit
    tests list these two routes explicitly; nothing else is allowed.
  - Values come from the existing engines (candles, price action, indicators, regimes) through
    `app.dashboard.services` on stored closed bars; nothing is re-implemented. Strategies are
    not run. Prices are raw (dividend-unadjusted) and labelled so; sessions are XNYS dates.
    NaN/inf become `null`, never 0; `insufficient_data` labels are reported as unavailable.
  - Point-in-time: only the latest closed session is described (swings with `confirmed_at`,
    events with `available_at` up to it; the engine's "as of last bar" zones are therefore
    valid). Indicator series are computed on the full history and sliced (causal engines).
  - Database access: one `SET TRANSACTION READ ONLY` transaction per request, rolled back at the
    end (verified: writes rejected inside it; the pooled connection is unaffected afterwards).
    Errors: database unavailable → 503 with a generic detail (exception type logged only), no
    stored data → 404, invalid range or parameters → 422; write methods → 405.
  - Engine outputs are cached in-process per dataset identity (app version, provider, bars,
    first/last bar, latest `ingested_at`): bounded LRU of 2, thread-safe, one computation per
    key at a time, failures never cached. A new ingestion changes the key.
  - Every response carries `meta.source = "LOCAL"` and the label "NOT LIVE · END-OF-DAY
    RESEARCH DATA"; the UI accepts API data only with that marker and keeps DEMO data separate.
  - Excluded: any import of broker, paper, operations, alerts, ingestion or data providers
    (tested); any write, order, account or configuration endpoint; real-time data.
- **Consequences:** The first request per dataset computes the engines (seconds); later
  requests are served from the cache. The UI shows stored historical research data, which is
  still not live market data. Further endpoints require a new ADR.

## ADR-0029 — Read-only research API (strategies, backtests, curves)

- **Status:** Accepted (2026-10-11, owner approval; frontend phase 4B)
- **Context:** The web UI needs the Phase 7/8 research results. Statistical validation, ML and
  walk-forward are explicitly out of scope for this step.
- **Decision:**
  - Exactly three new routes, all `GET`, under `/api/v1/research`: `/strategies` (fixed baseline
    rules, versions, features, parameters and the two benchmark definitions), `/backtests?slice=
    full|early|late` (default `full`; every Phase 8 metric of the six strategies and both buy &
    hold benchmarks) and `/curves?slice=…&kind=net|gross` (cumulative return and drawdown per
    series on the shared session axis). SPY only; unknown parameters → 422. The route
    whitelists list these three routes explicitly.
  - Results come from the existing backtester `run_suite` with the default `BacktestConfig`
    (next-session-open execution, generic cost assumptions labelled as such), the slice dates
    as `start`/`end`; strategies are computed on bars up to the slice end only. Nothing is
    re-implemented, tuned or ranked: series are listed in a fixed order (strategies, then the
    price and the total-return benchmark, which are never treated as equivalent).
  - Slices are the pre-declared Phase 9 date ranges and are reported with `out_of_sample: false`:
    the baselines are not fitted, so a slice is a descriptive sub-period, not a validation. They
    are restated in the API module (importing `app.validation` would load the validation engine)
    and a unit test asserts they equal `DEFAULT_SLICES`.
  - Gross curves use the same fills with zero costs (the engine's gross equity); their drawdown
    uses the engine's own drawdown definition. Undefined metrics are `null`, never 0.
  - Every response carries `meta.source = "LOCAL"`, the dataset key, the backtest and strategy
    configuration fingerprints, the window, the cost assumptions and when it was computed.
  - One `SET TRANSACTION READ ONLY` transaction per request, rolled back. Errors as ADR-0028:
    database unavailable → 503 (generic detail), no stored data or a slice without sessions →
    404, invalid parameters → 422, write methods → 405.
  - In-process caches: suites keyed by dataset identity + strategy engine fingerprint + backtest
    configuration (which contains the slice), bounded LRU of 6; serialized responses derived from
    a cached suite (keyed additionally by response and curve kind), LRU of 24. Same thread-safety
    and failure rules as ADR-0028 (one computation per key, failures never cached).
  - The dashboard research service is not imported (it would load the ML stack); the API uses
    the same underlying backtest functions, and equivalence with that service is tested. Same
    excluded imports as ADR-0028.
- **Consequences:** The first request per slice and dataset runs the full backtest suite
  (seconds); repeated requests are served from the cache. Validation, ML and walk-forward need
  further ADRs.

## ADR-0030 — Persistent registry of offline research runs

- **Status:** Accepted (2026-10-11, owner approval; Phase 5C)
- **Context:** Statistical validation and ML runs are computed offline and explicitly (Phase 5A,
  option B); their results must be stored so they can be read later without recomputation, and
  a reader must never mistake a partial, failed or altered run for a complete one.
- **Decision:**
  - `app/research_runs/registry.py`, a file registry under a caller-given root (git-ignored
    `data/` in practice): `index.jsonl` (append-only, hash-chained history reusing the alert
    store chain format), `staging/<run_id>/` while writing, `runs/<run_id>/` once published.
  - Run ids `{validation|ml}-<UTC timestamp>-<8 hex>`. Manifest `run.json`, format version 1:
    kind, start/end, config / data / dataset fingerprints as explicit `known | unknown |
    not_applicable` values (never guessed), app version, git commit and dirty state, parameters,
    and size + SHA-256 of every artifact.
  - Publication: artifacts written atomically to staging, manifest validated and written, hashes
    re-checked, staging renamed to `runs/`, then a `completed` event carrying the manifest
    SHA-256. COMPLETE requires that event, a matching manifest hash and every artifact matching;
    otherwise INTEGRITY_FAILED, FAILED (a `failed` event) or INCOMPLETE (no terminal event, also
    when a published directory lacks its `completed` event). Readers never write; writers refuse
    a corrupt history; invalid transitions are rejected against the history.
  - The hash-chain and atomic-write primitives move unchanged to `app/core/integrity.py`
    (on-disk format byte-identical; the alert, broker and operations stores keep their names).
  - Nothing is deleted automatically: failed staging is kept for diagnosis and unregistered
    directories are only reported.
- **Consequences:** Results are verifiable and attributable. Limits: no directory fsync on
  Windows; `rename` is atomic only on one volume (staging and runs share the root); INCOMPLETE
  does not distinguish "running" from "interrupted"; `git_dirty` counts untracked files.

## ADR-0031 — Recorded statistical-validation runs (`record` / `verify`)

- **Status:** Accepted (2026-10-11, owner approval; Phase 5D). Supersedes, for recorded runs
  only, the "outputs ephemeral" clause of ADR-0018; the ADR-0018 methodology is unchanged.
- **Decision:**
  - `python -m app.validation.cli record --symbol SPY --registry DIR` runs the existing
    `Validator` with the fixed `ValidationConfig` and records a `validation` run (ADR-0030):
    the five `ValidationReport.frames()` tables as CSV plus `summary.json`. `--registry` is
    required (no implicit location). The existing `run` subcommand is unchanged.
  - Bars and the dataset identity are read in one `SET TRANSACTION READ ONLY` transaction,
    rolled back. Inputs are checked before anything is registered; an engine or write error
    records the run as failed; a database error prints only the exception type.
  - `verify --registry DIR [--run-id ID]` only reads and re-checks history, manifest and
    hashes. Exit codes: 0 complete; 1 run not complete; 2 invalid input; 3 registry refused or
    unavailable (busy, corrupt, other registry or file-system error); 4 data unavailable.
  - One job per invocation, synchronous, only when invoked: no startup hook, schedule or HTTP
    route. The data fingerprint is the SHA-256 of the OHLCV + adj_close content and index (method
    recorded in the manifest).
- **Consequences:** The first real run (2026-10-11) is reproducible from its manifest, but it was
  recorded on a dirty tree (commit 144c593); later runs identify their code by commit. Reading
  recorded runs through the API needs a further ADR.

## ADR-0032 — PipelineLock serialised by an OS mutex

- **Status:** Accepted (2026-10-11, owner approval; Phase 5E). Refines the lock mechanism of
  ADR-0024; its semantics (owner file, automatic break only for a verified-dead local process,
  operator `unlock --force`) are unchanged.
- **Context:** On Windows a file cannot be deleted while another handle has it open: a contender
  reading the owner made `release()` fail and left a lock that could not be broken (its process
  was alive); concurrent stale-lock breaking also had a check-then-act window.
- **Decision:**
  - Acquire, stale-break, release and force-release run their short check-then-act sections
    under an exclusive OS lock (`msvcrt.locking` / `fcntl.flock`) on the persistent, never
    deleted `pipeline.lock.mutex` (the OS drops it if the process dies). Readers (`owner()`) do
    not take it.
  - Transient sharing errors are retried within a bound (2 s); waiting for the mutex is bounded
    (10 s, then `LockHeldError` / `LockReleaseError`). `LockReleaseError` (a `PermissionError`)
    leaves the lock held so `release()` can be retried; a lock whose owner cannot be read or
    confirmed is never deleted by `release()`; a failed owner write removes the new lock file.
  - No new dependency; public names unchanged.
- **Consequences:** Tested with concurrent threads and processes on Windows (no overlap, no
  abandoned lock, valid hash chains). Each lock root now contains an empty
  `pipeline.lock.mutex`. A program holding `pipeline.lock` open beyond the bound makes
  `release()` raise instead of hanging.

## ADR-0033 — Scheduler failure policy

- **Status:** Accepted (2026-10-12, owner approval; Phase 6). Refines the scheduler part of
  ADR-0024; the pipeline's own stage failure policy is unchanged.
- **Context:** The pipeline turns stage failures into results, but an exception raised outside
  the stages (store, lock release, an unexpected bug) propagated out of `Scheduler.tick()` and
  silently ended the long-running scheduler.
- **Decision:**
  - `tick()` catches `Exception` only from a scheduled run; `KeyboardInterrupt`, `SystemExit`
    and other control exceptions always propagate (Ctrl+C still stops the loop).
  - A failed run is logged once (`operations.scheduled_run_failed`; single line, URL
    credentials masked, at most 300 characters) and written to the heartbeat:
    `last_run_status = RUN_ERROR`, `last_error`, `consecutive_failures`, `retry_not_before`.
    It is never reported as a successful or no-op run.
  - Continue only when safe: ordinary failures keep the scheduler polling, but it does not try
    again before `poll × 2^(n−1)` seconds (capped at 1 h); a successful run resets the count.
    It **halts** after 3 consecutive failed runs, or immediately on `LockReleaseError` or
    `OpsStoreError`, whose lock or store state is uncertain; `ops status` then reports
    `halted: …` until an operator restarts it.
  - Locks: released by the pipeline's own `finally`; the scheduler never touches the lock.
  - Lock contention vs uncertainty: an OS-mutex timeout is `LockMutexTimeoutError` (a
    `LockHeldError` subclass): the run is still `LOCKED`, but the alert says the lock could not
    be checked instead of claiming that another pipeline holds it.
- **Consequences:** A transient fault no longer stops daily operations silently, and a
  persistent one cannot loop: at most 3 attempts with growing spacing, then a visible halt.
  Restarting a halted scheduler is a deliberate operator action.

## ADR-0034 — Risk mechanism semantics: stop, drawdown lock and their events

- **Status:** Accepted (2026-10-13). Owner decisions D-A1 to D-A9 (Step 8G); technical
  decisions consolidated after the Step 8H review (Step 8I) and the Step 8J implementation
  audit (Step 8K: equity zero, versioning, `exit_causes`, paper compatibility, implementation
  sequence). **Implementation status: implemented in the working tree, NOT committed**
  (2026-10-14): every decision D-A1 to D-A9, the equity-zero and undefined-return contracts,
  the per-mechanism semantic versions and paper state schema 2 — see *Implementation record*.
  The "Current behaviour" column below records the code BEFORE this implementation (kept as
  the decision record). The operational upgrade of existing paper accounts is **not** done
  (preconditions in *Paper compatibility*). Refines ADR-0020 (risk overlay)
  for `atr_stop_3` / `drawdown_lock_20` and the paper path.
- **Context:** The statistical review (Steps 8D/8E) moved the evaluation of the event-driven
  overlays from a Sharpe-difference test to deterministic validation of the mechanism plus a
  descriptive per-event report, so every mechanism rule must be unambiguous. The Step 8F
  review found behaviours contradicting the intended semantics (D-A1 exit attribution, D-A2
  the 20% boundary, D-A8 the lock counter), silent states (D-A3, D-A5) and unwritten
  conventions (D-A4, D-A6, D-A7, D-A9). Step 8H found that the paper ledger records no stop
  flag, so the `RISK_STOP_TRIGGERED` alert (`app/alerts/rules.py`) misses a stop coinciding
  with a strategy exit and repeats on every close of a rejected exit.

### Decisions

| Id | Adopted behaviour | Current behaviour (code) | Change required |
|---|---|---|---|
| D-A1 | **Stop events are per position episode.** Five distinct notions: the stop *condition* (`close ≤ level` on a close while holding: `stop_triggered`, may repeat); the stop *activation* (the **first** condition of the episode: new `stop_activation`); the strategy's exit request (request 0 while holding); the *risk decision reason* (`reasons` / `intervention_reason`, unchanged: why the approved target differs from the request); the *order* and the confirmed *fill*. The episode state (`stop_activated`) persists until an exit fill is confirmed and is reset by an entry or exit fill; while the exit is pending or rejected, further conditions are *repeated attempts*, never new activations. A stop on the same close as a strategy exit is an activation, with **one** exit at the next open. Exit attribution goes into an **additive** field `exit_causes` (every cause present, e.g. stop and strategy); `exit_reason` keeps its current meaning, documented as the *risk decision reason* of the exit. | One exit; `stop_triggered` true and summed in `stop_triggers`; no decision reason in the coincident case; `exit_reason = strategy_or_sizing` there. The paper `Decision` has no stop field, so the coincident case leaves no trace in the ledger and raises no alert; a rejected exit re-triggers and re-alerts on every close. | `stop_activation` on decisions (research and paper), `stop_activated` in `RiskManager`; `exit_causes` on research and paper trades; event counts, the research `intervention_events` and the `RISK_STOP_TRIGGERED` alert based on activations. |
| D-A2 | **Direct comparison with the level, no tolerance.** `lock_level = hwm × (1 − lock)` and `warning_level = hwm × (1 − warning)`, computed in float64; `LOCKED` iff `equity ≤ lock_level`, `WARNING` iff `equity ≤ warning_level` (not locked). Exact equality with the computed level activates; the next representable value above (`nextafter(level, +∞)`) does not; below activates. The rounding of the product (≤ ½ ulp) is numerical precision, not policy; no tolerance is introduced because it would move the policy threshold. The reported `drawdown` stays `equity / hwm − 1` (diagnostics, `max_drawdown`): the state is decided by the level, not by the ratio, which may read `−0.19999999999999996` on a locked close. Same class in research and paper. | `drawdown ≤ −lock` on the ratio: equity 80 with HWM 100 gives `−0.19999999999999996` and does **not** lock; the warning likewise. | `DrawdownMonitor`; boundary tests with `nextafter` for several HWM values, for both thresholds; research/paper parity on a boundary path. |
| D-A3 | **No unprotected entry.** When the profile requires an ATR stop and ATR(T) at the decision is non-finite or ≤ 0, a request that would **open** a position is approved at 0 with reason `stop_input_undefined` (same principle as `risk_input_undefined` for sizing). An open position is unaffected (its level was fixed at entry). `fixed_pct` needs no ATR. | The entry executes; `stop_level` returns NaN; the position has no stop and nothing reports it (the `stops.py` docstring wrongly says "reported"). | Check in `RiskManager.decide`; diagnostic count; docstring and `risk-management.md` updated with the code, not before. |
| D-A4 | `stop_lockout` is released **only** by a valid `FLAT` signal; `INSUFFICIENT_DATA` never releases it. | Compliant by construction (`INSUFFICIENT_DATA` keeps the previous request; a 0 request after an activation can only come from `FLAT`). | Tests (L → activation → I → L stays blocked; → F releases); then `strategy_state` is passed explicitly to `RiskManager.decide` (keyword-only, validated) and the lockout is released only on `FLAT`; both callers change (`app/risk/engine.py`, `app/paper/trader.py`); output-neutral (deterministic references). |
| D-A5 | **Invalid financial state fails explicitly:** non-finite or negative equity, non-finite or ≤ 0 high-water mark → `ValueError`; never a valid-looking result, and a failure never yields an artifact marked `complete`. Equity **equal to zero is valid** at a close and locks; every quantity that divides by equity is defined for it (see *Equity zero*). | Silent: NaN equity makes every comparison false, the state stays `NORMAL`, `max(hwm, NaN)` keeps the HWM, the run continues. Equity 0 is undefined: the research engine's `position = shares·close/equity` yields NaN (numpy), the paper trader's exposure raises `ZeroDivisionError` (Python), the session monitor divides by the previous equity. The paper state stores NaN as `null` and reads it back as NaN. | Checks in `DrawdownMonitor.update`, `SessionMonitor.update` and the close step of both engines; the equity-zero definitions below; in paper the exception marks the store broken (existing fail-closed path), before any NaN could be persisted. |
| D-A6 | A further fall during the cooldown neither extends nor restarts the lock in this version. The lock covers exactly `cooldown_sessions` decisions **including** the triggering close (21 for `drawdown_lock_20`); the next close unlocks (`NORMAL`), resets the HWM to the current equity (even if below the previous HWM) and may re-enter at the following open. **Exception, equity 0 at that close** (D-A5): the HWM is **not** reset to 0 — the last positive HWM is kept, so the drawdown stays −1 — and the close is judged by the threshold rule like any other (−1 ≤ −`lock`): a new lock starts; the end of a cooldown never allows a re-entry by itself, and equity 0 allows no entry or increase. | Compliant (the threshold is not re-evaluated while `lock_remaining > 0`). | Tests counting the locked decisions, the unlock close and the re-entry; deeper fall during the cooldown. |
| D-A7 | Every simulation window has its own risk state: flat, initial capital, HWM = initial capital, no stop, no lockout, no lock. Indicators and strategy states are warmed up causally on bars ≤ each decision (bars before the window allowed, none after). If the state at the bar before the window is `LONG`, the first open of the window is an entry with its own stop (D-A3). | Compliant (`simulate` builds cash = initial, shares = 0 and a new `RiskManager` per run). | Tests: window starting in a LONG regime; two runs in sequence equal each run alone. |
| D-A8 | **Separate counters:** `stop_activations` (D-A1), `drawdown_lock_events` (transitions of the **drawdown** monitor into `LOCKED` only) and `session_lock_events`. The existing `stop_triggers` keeps counting conditions. | `drawdown_lock_events` counts transitions of the combined `risk_state` (drawdown **or** session lock); no activation or session counter. | Per-session drawdown-monitor state in the equity frame; the new counters (additive columns). |
| D-A9 | **Paper:** a rejected or unexecuted exit is not a fill and clears no risk state (stop level, `stop_activated`, lockout, open trade, current target). The persisted paper state has an explicit schema version; the risk state gains `stop_activated` (state schema 2). A schema-1 state loads with `stop_activated = false` only when the profile has no stop; with an active stop it is **refused** (`StateError`) — the episode state is never inferred. | `on_fill` runs only on a confirmed fill (compliant for the existing fields); state schema 1 with a strict key set; no episode field. | `app/paper/state.py` schema 2 with the schema-1 loader rule; paper tests (rejected exit; coincident stop; schema-1 loads). |

### Equity zero (D-A5)

Equity equal to zero at a close is valid (it is not reachable under the accounting invariants
— positive validated prices, cash ≥ −tolerance — but the rule is defined, not assumed away):

- **Drawdown monitor:** `0 ≤ lock_level`, so the account locks (D-A2 rule); no division.
  At the end of a cooldown with equity 0 the HWM is not reset to 0 (which would make the next
  close an invalid state): the last positive HWM is kept and the close relocks (see D-A6).
- **No division by zero anywhere:** no quantity may become NaN or infinite, and no
  `ZeroDivisionError` may escape unhandled.
- **Normalised exposure / position** (research engine row, paper snapshot and trader): `0`
  when equity is 0 (there is nothing to normalise; holdings are then also worth 0).
- **No new entry:** with equity 0 no entry or increase is authorised (the approved target is
  not raised above the current one; a buy cannot be sized from zero equity anyway).
  Implemented with the reason `zero_equity_no_increase`; an open position is kept and an
  exit stays possible.
- **Session monitor:** when the *previous* equity is 0, the session loss is **not computed**
  (no division and no substitute number). The monitor keeps its protective state and blocks
  increases (it does not release or shorten an active session lock, and it treats the session
  as blocking increases). Telemetry must distinguish "not computable" from a loss of 0%.
- **Current limitation of the schema:** the session loss is returned by
  `SessionMonitor.update` and discarded; it is not recorded in decisions, equity rows,
  artifacts or the paper state, so no field can represent "not computable" today.
  Implemented (step 4a): `SessionMonitor.update` returns `float | None` (`None` = not
  computable); no session-loss telemetry is recorded. If it ever is, it is an additive,
  nullable field (`null`, never a number) with its meaning documented. No numeric value replaces an undefined percentage.
- **Daily return after a zero-equity close:** `daily_return(t) = equity(t) / equity(t−1) − 1`
  is **undefined** when `equity(t−1) = 0` — and for every following session while the equity
  stays 0. It is not computed (no division) and is represented as **null** (`None` in Python
  records, `null` in JSON, an explicit null token in CSV), never as 0, NaN, ±inf or an
  estimate. A close that *reaches* 0 from a positive previous equity has the defined return
  −1. The risk protection stays in force meanwhile (locked; no entry or increase until the
  risk conditions allow a release).
- **Consumers of daily returns — handling (implemented in step 4b):**
  - `returns_from_equity` (`app/backtest/metrics.py`) keeps its behaviour and serves the
    Phase 8 backtester, which is outside ADR-0034 (same accounting invariants). The risk and
    paper engines use `defined_returns`: no division where equity(t−1) = 0 (`pd.NA` there),
    and the same float64 value as `returns_from_equity` everywhere else (bit-identical).
  - In memory, a `float64` column cannot tell a null from a NaN. Decided and implemented: the
    `daily_return` column of `RiskRun.equity` / `PaperRun.equity` is a nullable `Float64`
    (`pd.NA` for undefined). Conversion to a plain float array is **not** a safe guard
    (`DataFrame.to_numpy(float64)` turns `pd.NA` into NaN silently), so every consumer checks
    explicitly (`undefined_mask`; a float64 NaN is non-finite, not undefined).
  - Run metrics (`all_metrics`) and the bootstrap comparisons (`diagnostics._compare`,
    `matrix_metrics`): an undefined return is never converted to 0 and never enters a
    computation as a valid return. A series containing one has its return-based metrics
    reported as undefined (null, with the reason) and the comparisons involving it are not
    computed ("not testable"); a series is never silently shortened to make it computable.
    Implemented: `annualized_volatility`, `sharpe` and `sortino` (and `gross_sharpe` for the
    gross series) are `None`, with `undefined_returns` and `return_metrics_note`; equity-based
    metrics (cumulative return, CAGR, drawdown) stay computed. `risk_table.csv` appends
    `annualized_volatility_status`, `sharpe_status` and `sortino_status` (step 4b.1), set from
    the calculation layer, never from an empty cell: `defined` (a finite value, zero
    included), `undefined_returns` (`None`: the series has undefined returns) or
    `undefined_nan` (NaN in the metric's own domain: fewer than 2 sessions, zero standard
    deviation, no negative return); one column per metric because these causes are
    independent (e.g. Sortino NaN with Sharpe defined). In `risk_comparisons.csv`, an undefined
    control makes every comparison of that strategy not testable, an undefined overlay only
    its own; the other comparisons are computed on the full sessions, bit-identical to a
    computation without the undefined series. Every row keeps its columns (NaN estimate and
    interval, `n_finite` 0, empty `estimate_in_interval`, `note` starting `not testable`).
  - `daily_returns.csv`: schema `risk-daily-returns/1` cannot represent a null (its fields
    are finite numbers or the tokens `nan`, `inf`, `-inf`). Implemented (step 4b): new runs
    write schema `risk-daily-returns/2`, same columns, with the explicit token `null` in
    `daily_return` for an undefined return, a convention text that says so and a per-series
    `undefined_returns` count in the manifest (`non_finite_returns` counts `nan`/`inf` only).
    The run verifier accepts a null only where the previous recorded equity is 0, refuses a
    number where it is 0, checks the counts, and skips nulls in the equity/return consistency
    check. `load_returns_matrix` refuses a series containing a null (`ReturnsMatrixError`) —
    no dropping, no imputation; complete series load as float64 as before. Schema `/1` files
    stay valid under their own contract (their convention; a `null` in a `/1` file is
    refused).
  - Statistical use (protocol `TI-STAT-RISK`): a window containing an undefined return makes
    the affected test *not testable* (an extension of validity condition 5, "no non-finite
    value"); sessions are never dropped to make a test testable.
- **Invalid states** (non-finite or negative equity, non-finite or ≤ 0 HWM) raise
  `ValueError`; a run that raises is `failed`, never `complete`.

### Versioning and fingerprints

- `RISK_VERSION` (`1.0.0`) and `PAPER_VERSION` (`1.0.0`) stay **unchanged** in this
  implementation, unless later evidence shows a change is indispensable (then it is proposed,
  not made silently).
- **Per-mechanism semantic versions** (`STOP_SEMANTICS`, `DRAWDOWN_SEMANTICS`,
  `SESSION_SEMANTICS`) are introduced. The payloads of `RiskConfig.fingerprint` and
  `PaperConfig.fingerprint` gain a `semantics` entry with the versions of the **enabled**
  mechanisms only, and only when that set is not empty.
- **What bumps a version:** D-A2 bumps `DRAWDOWN_SEMANTICS`; D-A3 bumps `STOP_SEMANTICS`.
  D-A1, D-A4 and D-A8 change no decision or fill (record schemas only). D-A5 bumps nothing
  **only if** (1) every finite, valid input gives identical decisions, fills and equity before
  and after (shown by the deterministic references of the implementation sequence) and
  (2) artifacts stay unambiguously identified by commit and `git_dirty`.
- **Acceptance criteria, to be confirmed by tests** (values computed from the current code;
  equal to the recorded research manifests and to the existing paper ledger):

  | Item | Value |
  |---|---|
  | research fingerprint of `control_no_overlay` | `3d5570e854d0e703` |
  | paper fingerprint of the control configuration | `411f10ae26cb281f` |
  | paper `account_id` (`sma_trend`, `1.0.0`, `SPY`, `1d`, control) | `3f95a6964b8e` |

  Expected to change: `drawdown_lock_20` and `atr_stop_3` (research and paper fingerprints,
  paper `account_id`). Expected unchanged: `control_no_overlay`, `fixed_fraction_50`,
  `volatility_target_10`, `atr_risk_1pct`.
- **If a compatibility criterion fails** (a control value changes, or D-A5 is not shown to be
  output-neutral), the implementation **stops** and a resolution is proposed; the control's
  identity is never changed silently. (A per-mechanism version cannot express a change that
  affects the control, which has no enabled mechanism; such a change needs an explicit
  decision.)
- Reuse of incompatible state is refused by existing checks: `open_account` recomputes the
  `account_id` from the current configuration and refuses a mismatching directory; the state
  loader and `PaperTrader` refuse a different `config_fingerprint`. Their messages ("not one
  of the pre-declared scenarios", `PAPER_STATE_CORRUPT`) should name a semantic-version
  mismatch.
- Run manifests keep the fingerprints they were written with; the run verifier compares
  against recorded values. Research manifests record the semantic versions (additive).

### `exit_causes`

- **Canonical string**: causes unique, sorted alphabetically, joined by `+` (e.g.
  `stop+strategy`, `drawdown_lock+stop`). Independent of evaluation order; CSV-friendly.
- **Closed vocabulary**: `drawdown_lock`, `session_lock` (reserved: while the session lock
  only blocks increases it can never cause an exit), `sizing` (the sized exposure is 0 for a
  positive request, e.g. `risk_input_undefined`), `stop`, `strategy` (the strategy requested 0).
- Never empty on an exit. A validator rejects unknown, repeated or non-canonically ordered
  causes.
- Derived from the decision that produced the exit order. `exit_reason` keeps its current
  meaning (the risk decision reason of the exit).
- **Pending stop exit vs new attempt (audit fix 1).** A decision that (re)produces an exit
  order while the stop episode is activated and its exit is not yet confirmed (pending or
  rejected) keeps `stop` among its causes, together with the causes of that new attempt
  (e.g. `stop+strategy` when the strategy turned FLAT meanwhile, even on a close above the
  level). The stop stops being a cause only when the confirmed exit (or entry) fill ends the
  episode; `zero_equity_no_increase` is never an exit cause.

### Paper compatibility

- The persisted paper state moves to **schema 2**, adding `stop_activated` (and the exit-cause
  information the pending instruction needs).
- A **schema-1** state loads only when the profile has **no stop**, with
  `stop_activated = false`; with an active stop it is **refused** (`StateError`) — never
  inferred. The next write stores schema 2.
- A rejected or unexecuted exit is not a fill and does not clear the stop state.
- The `RISK_STOP_TRIGGERED` alert uses `stop_activation` for new ledger records and keeps the
  current rule for records without it, so recomputing alerts over the existing history does
  not create new historical alerts.
- **Operational upgrades** require that no uncommitted ledger tail exists (the state's
  sequence equals the ledger length): crash recovery compares the tail with the reprocessed
  session, and a tail written by older code would not match.
- **Implemented reading rules** (`app/paper/state.py`): every write is schema 2
  (`risk.stop_activated`, `pending.exit_causes`). A schema-1 state loads with
  `stop_activated = false` when that is determined — the profile has no stop, or no position
  is open (no shares, no stop level, no open round trip) — and is **refused** when the profile
  has a stop and a position is open. The causes of a pending schema-1 exit are derived only
  where the decision rules determine them (request 0 → `strategy`, plus `drawdown_lock` when
  the stored drawdown state is LOCKED with `force_flat`; a positive request without a drawdown
  lock → `sizing`); a positive request under a drawdown lock is ambiguous and refused. These
  rules are checked before the configuration fingerprint, so a refusal names its reason.
- **Identity conflicts are explicit.** Neither the paper CLI nor the dashboard creates an
  account silently next to an account of the same strategy, risk scenario, instrument and
  timeframe under another account id (an earlier configuration, semantic version or strategy
  version): creating it requires an explicit decision about the existing account(s), which
  are named and not opened or changed. Both use the store's single manifest scan
  (`scan_accounts`, direct children of the account root only). The CLI `run` refuses with exit
  code 2 (`conflicting_accounts`); `status` is read-only and never creates an account. The
  dashboard's `paper_create_account` raises `AccountError` before anything is written, and
  ALSO refuses when an account directory has a manifest that cannot be read (its identity is
  unknown, never assumed); the CLI keeps its earlier behaviour and ignores such directories —
  a deliberate difference between the two paths. The alert monitor reports such an intact account as
  `PAPER_ACCOUNT_INCOMPATIBLE`, distinct from `PAPER_STATE_CORRUPT` (invalid or unreadable
  state) and `PAPER_LEDGER_VERIFICATION_FAILED`.
- **Not executed:** no existing (operational) paper account was opened or converted. Before
  any conversion: stop the scheduler, back up the account directories, confirm that no
  uncommitted ledger tail exists, and note that accounts of `drawdown_lock_20` or
  `atr_stop_3` (new `account_id`) are not reopened under the new semantics (their directories
  no longer match a pre-declared configuration). The control account's identity is unchanged
  and its schema-1 state loads under the rules above.

### Implementation record (working tree, not committed)

| Item | Implemented as | Evidence (unit tests) |
|---|---|---|
| D-A1 | `RiskManager.stop_activated` (set by the first condition, reset only by a confirmed entry or exit fill); `RiskDecision.stop_activation` and `exit_causes` (each cause evaluated on its own, not from the sequential reasons: `strategy` = request 0, `sizing` = sized 0 for a positive request, `stop` = condition at this close or active lockout, `drawdown_lock` = drawdown monitor LOCKED with `force_flat`; validated, never empty on an exit); research decisions and trades, paper `Decision`, `Instruction` and trades carry them (appended); `exit_reason` unchanged; `RISK_STOP_TRIGGERED` follows `stop_activation` on records that have it, the former rule otherwise. | `test_adr0034_mechanisms.py` (D-A1 group), `test_state_schema2.py`, step-0 `test_current_paper_rejected_exit` (one alert per episode) |
| D-A2 | `DrawdownMonitor`: LOCKED iff `equity <= hwm × (1 − lock)`, WARNING iff `equity <= hwm × (1 − warning)` (`lock_level`, `warning_level`); the reported drawdown stays the ratio. | `nextafter` boundaries for 5 HWMs and both thresholds; research/paper parity on the boundary path |
| D-A3 | `RiskManager.decide`: with an ATR stop, an entry (current target 0) whose decision ATR is NaN, ±inf, 0 or negative is approved at 0 with `stop_input_undefined`; an open position is unaffected; `fixed_pct` and no-stop profiles need no ATR. | D-A3 group; synthetic reference: the only D-A3 differences are entries refused during the ATR14 warm-up |
| D-A4 | `RiskManager.decide(..., strategy_state=...)` required, keyword-only, validated (vocabulary; LONG ⇒ request > 0; FLAT ⇒ request 0); only FLAT releases the stop lockout; both engines pass the state. | D-A4 group; output-neutral on the synthetic reference (0 differences) |
| D-A5 / D-A6 | Steps 4a, 4a-fix, 4b, 4b.1 (see *Equity zero*). | `test_da5_invalid_state.py`, `test_undefined_returns.py`, `test_risk_table_status.py` |
| D-A7 | Unchanged code; tests, including the paper window start (ADR-0035). | step-1 tests, `test_window_start.py` |
| D-A8 | Equity frame appends `drawdown_state` and `session_locked`; `drawdown_lock_events` = transitions of the drawdown monitor into LOCKED; `stop_activations` and `session_lock_events` appended; `stop_triggers` still counts conditions; `intervention_events` uses `stop_activations` and `session_lock_events`. | D-A8 group; the pre-declared profiles' existing counters are unchanged (no session lock) |
| D-A9 | Paper state schema 2 (see *Paper compatibility*). | `test_state_schema2.py`, step-1 fixture tests |
| Versions | `STOP_SEMANTICS` 2.0.0 (D-A3), `DRAWDOWN_SEMANTICS` 2.0.0 (D-A2), `SESSION_SEMANTICS` 1.0.0 (unchanged for valid inputs); a `semantics` entry in the risk and paper fingerprint payloads only for enabled mechanisms; research manifests record them per scenario; `RISK_VERSION` and `PAPER_VERSION` unchanged. | identity tests (step 1), payload tests |

**Fingerprints and paper `account_id` (`sma_trend`, `1.0.0`, `SPY`, `1d`), before → after:**

| Profile | Research | Paper | `account_id` |
|---|---|---|---|
| `control_no_overlay`, `fixed_fraction_50`, `volatility_target_10`, `atr_risk_1pct` | unchanged (control `3d5570e854d0e703`) | unchanged (control `411f10ae26cb281f`) | unchanged (control `3f95a6964b8e`) |
| `drawdown_lock_20` | `46967e83f8a3cbc8` → `989fe7971a707205` | `def3947a77643adc` → `5dc60575258be11f` | `1ecfb5f0843c` → `4d46afd9443d` |
| `atr_stop_3` | `f66a5a3d801afdc8` → `99566717f29b5310` | `3eb9676335813ff5` → `b48a0c434b4de2ef` | `7ca091f382ce` → `62d66e3174d3` |

**Step-0 references.** The `BASELINE` constant is not rewritten (it remains the pre-ADR
record). The intended changes are recorded next to it (`ADR0034_INTENDED`) and asserted to
differ from it: `drawdown_exact_threshold` (D-A2, all four frames) and `paper_rejected_exit`
(identifiers only, through the new `atr_stop_3` paper identity; with the pre-ADR identity
substituted, every frame equals its baseline). Every other frame of every scenario is
unchanged. On the synthetic reference set (216 research and paper runs, frozen columns and
fields only), the only differences are 10 `atr_stop_3` runs starting at session 0, all
explained by D-A3 (entries refused only where the decision ATR is undefined).

### Implementation sequence (proposed, not executed)

Each step has its own commit, with the full unit suite, Ruff and mypy; any reference or
acceptance difference not listed as intended reverts that step.

1. Deterministic references of decisions, fills and equity — synthetic scenarios and isolated
   tests only, recorded with the current code.
2. Tests fixing the control's research fingerprint, paper fingerprint and `account_id`.
3. Tests for the rules already met: lockout (D-A4), cooldown (D-A6), windows (D-A7), paper
   state on rejected exits (D-A9).
4. D-A5, including equity zero.
5. Per-mechanism semantic versions and the drawdown thresholds (D-A2).
6. ATR validation and refusal of new entries (D-A3).
7. Stop events per episode, `exit_causes` and separate counters (D-A1, D-A8).
8. Paper state schema 2 and alert compatibility.
9. `strategy_state` as an explicit argument of `RiskManager.decide` (D-A4).
10. Final documentation, only after every test passes.

### Consequences

- Code changes: D-A1, D-A2, D-A3, D-A5, D-A8, D-A9 (state schema) and the fingerprint payloads.
  Test-only: D-A6, D-A7. D-A4: tests, then the explicit `strategy_state` argument.
- Additive record changes: `stop_activation` on decisions (research and paper ledger records;
  old ledger records are unchanged, so the hash chain is unaffected), `exit_causes` on trades,
  new diagnostics counters. Ledger readers access fields by key; their tolerance to the new
  fields must be confirmed by tests.
- Documentation to update with the code, not before: `docs/risk-management.md` (stops,
  drawdown lock, events), `docs/paper-trading.md` (state schema 2) and the `stops.py`
  docstring (D-A3).

### Open questions

- Whether the D-A5 no-bump conditions hold — decided by the deterministic references, not
  assumed. Evidence so far (steps 4a, 4b): the 11 step-0 baselines and a set of 216
  synthetic research and paper runs are unchanged, and the bootstrap comparisons are
  bit-identical; no version was bumped.
- ~~Whether ledger readers tolerate the new decision fields~~ — confirmed by tests (alerts,
  dashboard ledger tables, broker `expected_quantity` read the new records).
- Whether session-loss telemetry is to be recorded at all (see *Equity zero*) — not
  recorded (decision F of the step 4a authorisation); still open for a later decision.
- Whether the historical SPY research results in `docs/risk-management.md` are recomputed
  under the new semantics — not done (no operational backtest was authorised).
- ~~In-memory representation of an undefined daily return~~ — resolved in step 4b:
  nullable `Float64` with `pd.NA`.

### Revision history

| Date | Step | Change |
|---|---|---|
| 2026-10-13 | 8G | ADR created: owner decisions D-A1 to D-A9 with current behaviour and required changes. |
| 2026-10-13 | 8I | Technical decisions consolidated: stop events per episode with `stop_activation` and additive `exit_causes`; D-A2 by direct comparison with the level (the earlier proposal of a `1e-12` tolerance on the ratio is withdrawn) and applied to the warning; invalid-state rules completed (negative equity, HWM ≤ 0, zero equity valid); separate counters; paper state schema 2 without inference; per-mechanism semantic versions (the earlier plan to bump `RISK_VERSION` is withdrawn); conditional D-A5 no-bump rule. |
| 2026-10-13 | 8K | Equity zero defined (valid, locks; exposure 0; no new entry; session loss not computed, protection kept; schema limitation and minimal change documented); `RISK_VERSION`/`PAPER_VERSION` unchanged; control acceptance values; stop-on-failure rule; `exit_causes` vocabulary and canonical form; paper schema 2 and upgrade precondition; implementation sequence (proposed). Closed: the open questions on `exit_causes`, the global `RISK_VERSION` and D-A4 (explicit `strategy_state`). |
| 2026-10-14 | 1-prep | Daily return after a zero-equity close specified (undefined, null, never 0/NaN/inf/estimate; −1 when reaching 0; protection kept); consumers' required handling; `daily_returns.csv` cannot represent null today — minimal change documented (schema `risk-daily-returns/2`, not implemented); statistical tests with an undefined return are not testable. No accepted decision changed. |
| 2026-10-14 | 4a-fix | D-A5 × D-A6 contradiction resolved: at the end of a drawdown cooldown with equity 0 the HWM is not reset to 0 (last positive HWM kept, drawdown −1) and the close relocks under the threshold rule; no re-entry from the end of the cooldown itself. Found in step 4a (a zero HWM made the next close fail). Positive-equity behaviour unchanged. |
| 2026-10-14 | 4b/4b.1 | Status updated (implemented in the working tree, not committed; pending items listed). Recorded as implemented: `zero_equity_no_increase` (approved name), `SessionMonitor.update` → `float | None`, `defined_returns` with nullable `Float64`, undefined return-based metrics, not-testable comparisons, `risk-daily-returns/2` (`null`, `undefined_returns`, verifier and loader rules, `/1` kept), and the `<metric>_status` columns of `risk_table.csv` (`defined` / `undefined_returns` / `undefined_nan`, one per metric). Open question on the in-memory representation closed. No decision changed. |
| 2026-10-14 | final | Implementation completed in the working tree (not committed): D-A1 (`stop_activated`, `stop_activation`, `exit_causes`, alert per activation), D-A2 (level comparison), D-A3 (`stop_input_undefined`), D-A4 (required `strategy_state`, FLAT-only release), D-A8 (monitor columns, separate counters, activation-based events), paper state schema 2 with the schema-1 reading rules, per-mechanism semantic versions (stop and drawdown 2.0.0, session 1.0.0) and the resulting identities. Implementation record, fingerprints before/after and the not-executed operational upgrade added. No decision changed. |
| 2026-10-14 | audit-fixes | (1) `stop` stays an exit cause while an activated stop's exit is unconfirmed; (2) the paper CLI refuses to create an account next to one of the same strategy, scenario, instrument and timeframe under another identity, and `status` is read-only; (3) alerts classify an intact state of another configuration / semantic version / schema as `PAPER_ACCOUNT_INCOMPATIBLE` (`StateCompatibilityError`), separate from corruption; (4) `max_position_notional` must exceed 1e-12 × the initial equity (refused at `RiskManager` construction), with an explicit error if the equity later grows past it. No decision changed. |
| 2026-10-15 | dashboard | The dashboard's `paper_create_account` refuses (before writing anything) to create an account next to one of the same strategy, scenario, instrument and timeframe under another identity, and when an account manifest cannot be read; the single manifest scan is `scan_accounts`, with `conflicting_accounts` (CLI) returning its conflicts only. The CLI keeps ignoring unreadable manifests (deliberate difference). No decision changed. |

## ADR-0035 — Paper window start: decision at the close before `start` (convention A)

- **Status:** Accepted (2026-10-14). Convention A (A2) and the indicative dashboard
  classification (D1) are **implemented in the working tree only; not yet committed** (no
  commit contains them; integration in a commit is pending). Files:
  - A2 (paper trading): `app/paper/trader.py` (`PaperTrader.prime`, `split_at_start`),
    `app/paper/engine.py`, `app/paper/store.py`, `app/paper/cli.py`,
    `app/dashboard/services/paper.py` (`paper_process_sessions`); tests
    `tests/unit/paper/test_window_start.py` and the updated window-start test in
    `tests/unit/risk/test_adr0034_preservation.py`.
  - D1 (dashboard): `app/dashboard/services/paper.py` (`classify_snapshots`,
    `ledger_tables`), `app/dashboard/ui/pages.py` (caption of the *Account snapshots* tab);
    tests `tests/unit/dashboard/test_snapshot_types.py`.

  Refines ADR-0021 for new accounts with a `start`; ADR-0021 is otherwise unchanged.
- **Context:** The Step 1 audit of ADR-0034 found that a paper window and a research window
  (`RiskOverlay.run(start_index=k)`, ADR-0034 D-A7) with the same `start` diverged. The paper
  replay, CLI `run --start` and the dashboard's `paper_process_sessions` dropped the sessions
  before `start` and began with a requested target of 0, so the first decision was taken at the
  close of `start` and a LONG regime entered one session late (three sessions late in the
  audited case with `INSUFFICIENT_DATA` in the first two window sessions). The research engine decides at the close of
  `start−1` and enters at the open of `start`.
- **Decision (convention A, ledger representation A2):**
  1. `start` is the first session included in the results.
  2. When a session exists before `start`, the decision at its close may execute at the open of
     `start`.
  3. The history before `start` rebuilds the requested target causally (LONG 1, FLAT 0,
     `INSUFFICIENT_DATA` keeps the previous request, 0 before any valid state); nothing after
     `start−1` is read.
  4. The window starts with the initial capital and no position, cost basis or stop. A LONG
     request at `start−1` is a new entry at the open of `start`, with the normal costs and its
     own stop (ATR of the `start−1` decision).
  5. The `start−1` decision is made with equity equal to the initial capital and does **not**
     update the risk monitors (no valuation close in the window). It creates no result row.
  6. It is recorded with the existing schemas: a `decision` event and a **reference snapshot**
     (flat, cash = equity = initial capital, marked at the `start−1` close, with the pending
     kind just created). The pair keeps the per-session ledger invariant (decision, snapshot),
     so every ledger reader (broker intent and reconciliation, alerts, operations) works
     unchanged. The reference snapshot exists for traceability and execution only.
  7. Results start at `start`: the replay's equity, daily returns, metrics, portfolio and trades
     exclude `start−1`; `decisions` includes the `start−1` decision, as the research
     engine's decision frame does. `session_seq` / `sessions_processed` count window sessions
     only.
  8. Without a session before `start`, there is no previous decision (unchanged boundary
     behaviour). If the session immediately before `start` is missing from the data while
     window sessions exist, the window start fails (`ValueError`), as the research engine does.
     In incremental mode, the `start−1` decision is only taken once that session is available
     (an earlier last session means nothing is processed yet).
  9. Only new accounts are affected: an account that has processed any session ignores `start`;
     no existing account, ledger or state changes. Ledger and state schemas, `app/broker/`,
     configuration fingerprints, account ids and the ADR-0034 Step 0 baselines are unchanged.
- **Consequences:**
  - A paper window equals the research window with the same `start` (sessions, fill prices,
    equity within 1e-6, decisions, risk states), tested for the control, `atr_stop_3` and
    `drawdown_lock_20` profiles, with and without costs. A replay with `start` equals an
    incremental account with the same `start` (ledger events, IDs and hashes), including
    restarts and a crash after the reference events.
  - The ledger of an account started with `start` is identical (including hashes) to that of a
    new account whose first session is `start−1` and that processes the same sessions. Only the
    state's `session_seq` differs. The records themselves do not mark a snapshot as a reference
    (see *Open questions*).
- **Dashboard classification (D1, implemented):** the *Account snapshots* table shows a
  display-only session type computed by `classify_snapshots` (`app/dashboard/services/paper.py`)
  from the whole committed ledger and the account's `sessions_processed`: `trade` only with a
  `fill` event in the same session; `no trade` without one (an order may still have been
  rejected); `window-start reference (indicative)` for an account's first snapshot without a
  fill when the snapshot count is `sessions_processed + 1` and the snapshot is flat at the
  initial capital; `indeterminate` when the data do not allow a classification. Nothing is
  persisted; no schema, ledger hash, fingerprint or baseline changes. D1 is an **indicative**
  classification: it does **not** identify the reference snapshot per record with certainty
  (the same ledger is shown as `no trade` for an account whose first session is `start−1`).
  The running Streamlit page has not been validated (service-level tests only).
- **Open questions:** A reference snapshot still cannot be told apart **per record** from a
  normal flat session that schedules an entry. The options to mark it per record (D2 additive
  snapshot field, D3 distinct `snapshot_id`, D4 account-level record in the state or manifest)
  are described in `docs/paper-trading.md` (*Window start*); they are **not implemented and
  not approved**.

### Revision history

| Date | Step | Change |
|---|---|---|
| 2026-10-14 | A2 | ADR created: convention A with the decision plus reference snapshot representation (A2); results start at `start`; boundary, missing-session and existing-account rules; snapshot distinction left open. |
| 2026-10-14 | D1 | Dashboard classification D1 recorded as implemented (indicative, from the whole committed ledger and `sessions_processed`; not a per-record identification); D2–D4 remain not implemented and not approved. The window-start decision is unchanged. |

## ADR-0008 — Local Git now, private GitHub remote later

- **Status:** Accepted (2026-10-07)
- **Decision:** Local repository on `main`. `.gitignore` excludes `.env`, secrets and all data
  formats; `.env.example` documents configuration. The private GitHub remote is added by the owner
  (instructions in README). CI is deferred.

## ADR-0036 — Forex product scope, environments and agent architecture

- **Status:** Accepted (2026-10-17). Implemented on branch `feature/fx-desktop-platform`
  (`app/fx`). The SPY research platform is unchanged and kept alongside.
- **Context:** The owner pivoted the project to a personal Forex trading platform with five
  agents, a central orchestrator and a central risk manager, intended to become a product that
  can later be sold or offered by subscription. The existing paper model (long-only, exposure in
  [0, 1], one instrument, daily bars) cannot represent FX (shorts, margin, bid/ask, several pairs,
  account-currency conversion, a 24/5 calendar).
- **Decision:**
  1. New packages: `app/fx` (domain, agents, risk, orchestrator, simulator, store), `app/engine`
     (process + protocol, ADR-0037), `app/desktop` (UI). Existing packages are reused only for
     pure functions (`app.indicators`) and the real-money guard (`app.core.safety`).
  2. **Environments** are explicit and never interchangeable: `SIMULATION` (local synthetic
     market + simulated broker), `DEMO` (a broker's practice account, no real money) and `REAL`.
     `REAL` exists only as an identifier: `require_executable(REAL)` calls
     `refuse_real_money_order()` (ADR-0006) and the configuration refuses it. No adapter falls
     back from one environment to another. A DEMO adapter must trade LIVE-origin prices only.
  3. **Ports** (`app/fx/ports.py`): `MarketDataProvider`, `AccountProvider`, `PositionProvider`,
     `OrderExecutionProvider` (idempotent by client order id; `ExecutionError(ambiguous=...)`),
     `TradingCalendar`, plus declared `Capabilities`.
  4. **Every price carries its origin** (`SYNTHETIC`, `HISTORICAL`, `DELAYED`, `LIVE`); the UI
     labels synthetic prices on every screen.
  5. **Agents** propose, they never trade: Trend Following (EMA 20/50 + slow-EMA slope), Momentum
     (RSI/ROC/MACD agreement), Mean Reversion (Bollinger + RSI), Volatility/Regime filter
     (efficiency ratio, ATR percentile, spread/ATR; can veto entries), Portfolio Coordinator
     (regime-weighted consensus, conflict = no trade, currency-diversification penalty, at most
     one new entry per cycle). Agents read closed bars only (tested for look-ahead).
  6. **Scores** are heuristic confidences in [0, 1], never probabilities; the UI says so.
  7. **Order lifecycle** (`app/fx/lifecycle.py`): SIGNAL → PROPOSAL → AUTHORIZED | REFUSED →
     SENT → ACCEPTED | REJECTED | UNKNOWN → PARTIALLY_FILLED → FILLED (+ CANCELLED, EXPIRED).
     Each step is journaled; an UNKNOWN (ambiguous) send is resolved by looking the order up by
     client order id, never by resending. The intent is written to disk (SENT) before the venue
     sees it, and the outcome is saved immediately after.
     *Revision 2026-10-18 (review fixes E3, M1, M6):* every state change goes through
     `lifecycle.transition` (`Intent.move`); terminal states have no way out. An empty lookup
     proves absence only if the adapter declares `authoritative_order_lookup` (the in-process
     simulator does); otherwise the order stays UNKNOWN, new entries on its pair are refused,
     lookups are retried on each tick up to `ReconcilePolicy` (5 attempts / 5 min of engine
     clock) and then the kill switch is engaged with an `order_unresolved` alert, the order
     staying UNKNOWN. Orders still unknown after a restart block every new entry until
     reconciled. A partial fill whose remainder is cancelled ends CANCELLED with
     `filled_units > 0` (PARTIALLY_FILLED is reserved for a working order); fill quantities
     are the venue's cumulative totals, so repeated reports never double-count.
  8. **Accounting**: netting per pair (no hedging), P&L in the quote currency converted to the
     account currency (direct, inverse or USD cross), unrealized marked at the liquidation side,
     margin = notional / venue leverage. Swap/financing is not modelled yet (limitation).
  9. **Persistence**: SQLite per data directory; hash-chained journal + state documents written
     in one transaction (market, broker, risk, orchestrator). A restart recovers positions,
     daily counters, locks, the kill switch and in-flight intents.
- **Provisional defaults (configurable):** account EUR, capital 10 000, nine major pairs,
  decisions on M15 bars, venue leverage 30 (ESMA retail majors).
- **Not done yet:** DEMO broker adapter and live data provider (require choosing a provider and
  the owner's own practice account; no account was created and no credential used).

## ADR-0037 — Native desktop application and engine process protocol

- **Status:** Accepted (2026-10-17). Implemented in `app/desktop` and `app/engine`.
- **Context:** The owner requires a native Windows application in its own window (resizable,
  full screen, icon), game-like, packageable as an `.exe`, not a web app in a browser. Godot 4
  and PySide6 were compared in Phase 0.
- **Decision:**
  1. **PySide6 (Qt 6, LGPL v3)**: one language and one process model with the Python engine,
     native widgets for dense trading tables, procedural pixel art in `QPainter`. Godot would add
     a second language/runtime and an IPC bridge for little gain at this stage.
     `pyside6-essentials` only (no WebEngine).
  2. **Separate engine process.** The UI starts the engine as a child process (`--engine` in the
     frozen exe, `python -m app.engine` from source) and is only a client: it renders snapshots
     and sends commands; it never places orders. A UI crash cannot corrupt the engine state.
  3. **Protocol v1** (`app/engine/protocol.py`): JSON lines over TCP on 127.0.0.1 only; a fresh
     random session token per launch, passed through the environment (not the command line) and
     compared in constant time; `hello/welcome`, periodic `snapshot`, live `event`, `command`
     with id and `ack`, `heartbeat`. There is no command to place an order by hand. Destructive
     commands need typed confirmation words (`FECHAR`, `REINICIAR`, `LIBERTAR`).
  4. **Single instance** per data directory (OS lock on `engine.lock`).
  5. **Window-close policy:** closing the window asks for confirmation, then stops the engine
     cleanly (state saved; positions stay in the simulated account with their server-side stops
     and are recovered on the next start). *Revision 2026-10-18 (E2, M2-M5):* the engine
     refuses to start (exit code 4, `ENGINE_ERROR startup:<code>`, never restarted
     automatically by the desktop) when the configured environment, the adapter's declared
     environment and the enabled set (`environment.ENABLED` = SIMULATION) disagree, when the
     journal hash chain is broken or shorter than the saved state's journal head, when state
     documents are malformed/incomplete (or missing while the journal has history), or when
     the configured account currency differs from the persisted account's; nothing is
     repaired or recreated. The desktop's environment badge shows only the engine's validated
     environment. An internal error pauses the engine with a persisted `safety` reason; only
     the explicit `resume` command resumes. **Orphan policy:** the engine stops by itself if no
     client is connected for 20 s, so a crashed UI never leaves a hidden engine trading. A
     background/tray mode is a possible later option (it needs its own ADR).
  6. **Visual identity:** dark cyber operations room, neon green/magenta/cyan, amber warnings, red
     critical; all art procedural (no third-party assets). **State is never shown by colour
     alone**: every status has a distinct glyph shape and a text label.
  7. **Packaging:** PyInstaller one-folder build (`packaging/build.py`), Windows version
     resource, generated multi-size icon, portable zip. LGPL compliance by dynamic linking and
     `THIRD_PARTY_NOTICES.md`. Code signing is not set up (SmartScreen will warn).

## ADR-0038 — FX trading day and daily risk definitions

- **Status:** Accepted (2026-10-17). Implemented in `app/fx/calendar.py` and `app/fx/risk.py`.
- **Decision:**
  1. **Trading day** = 17:00 New York to 17:00 New York (rollover), DST-aware through `zoneinfo`.
     The market is open Sunday 17:00 to Friday 17:00 New York; configurable holiday trading days
     (25 December and 1 January by default) are closed.
  2. **Daily P&L** = current equity − day-start equity, where equity = balance + unrealized P&L
     marked at the liquidation side; it is therefore net of spread, commissions and realized
     results. Day-start equity is the first equity observed in the trading day.
  3. **Entries**: an authorized intent that opens or increases exposure (including the opening
     leg of a reversal) counts as one entry. It is reserved at authorization, consumed when it
     fills (a partial fill consumes it) and released if nothing fills; a reservation in flight at
     the rollover counts on the day it fills. Exits never count. Default maximum: 5 per day.
  4. **Locks**: daily P&L ≤ −5 % → `daily_loss` lock (default: flatten positions); ≥ +15 % →
     `profit_target` lock (default: keep positions with their stops). A lock blocks new entries
     until the next trading day, survives restarts and cannot be released by hand. Exits are
     always allowed. **Precedence** (revision 2026-10-18, fix E1): `daily_loss` >
     `profit_target`. The loss limit is evaluated on every observation even after the profit
     target locked the day; reaching it upgrades the lock to `daily_loss` and applies its
     flatten policy. A `daily_loss` lock is never downgraded.
  5. **Kill switch**: blocks all new entries until released manually with the word `LIBERTAR`;
     survives restarts and day rolls; may be engaged with "flatten all".
  6. **Entry checks** (all must pass): environment not REAL; kill switch off; no lock; entry
     available; market open and outside 15 min around the rollover; fresh quote (≤ 120 s); price
     origin allowed (DEMO: LIVE only); spread ≤ 3 pips; score ≥ 0.55; stop loss present and on
     the right side; risk/reward ≥ 1.2; size from risk per trade (0.5 % of equity at the stop),
     cut to the leverage cap (30× gross); minimum size; max open positions; free margin after
     the trade ≥ 50 % of equity; net exposure per currency ≤ 600 % of equity. No pyramiding.
  7. Changing risk settings applies immediately but never clears an engaged lock or the kill
     switch; account currency/capital change only through an explicit account reset.
