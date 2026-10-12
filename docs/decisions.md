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

## ADR-0008 — Local Git now, private GitHub remote later

- **Status:** Accepted (2026-10-07)
- **Decision:** Local repository on `main`. `.gitignore` excludes `.env`, secrets and all data
  formats; `.env.example` documents configuration. The private GitHub remote is added by the owner
  (instructions in README). CI is deferred.
