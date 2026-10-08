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

## ADR-0008 — Local Git now, private GitHub remote later

- **Status:** Accepted (2026-10-07)
- **Decision:** Local repository on `main`. `.gitignore` excludes `.env`, secrets and all data
  formats; `.env.example` documents configuration. The private GitHub remote is added by the owner
  (instructions in README). CI is deferred.
