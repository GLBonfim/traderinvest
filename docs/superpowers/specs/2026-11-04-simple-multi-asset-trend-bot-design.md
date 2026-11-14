# Simple Multi-Asset Trend Bot — Design

Date: 2026-11-04  
Status: superseded in part by the
[2026-11-14 design v2](2026-11-14-multi-asset-trend-v2-design.md) (ADR-0040). Principles,
safety boundaries and later operational stages remain valid; the strategy, universe, gates and
delivery plan are replaced by v2.

## Intent and why

Replace the current search for a profitable fast Forex agent with a deliberately small,
auditable research and execution path. The new bot should make one understandable decision at
a slow cadence, account for every real cost in EUR, and refuse to trade when the evidence or
operations are not good enough.

The goal is not a high hit rate and not a promise of profit. The goal is evidence of positive
net expectancy after costs, followed by controlled evidence that the same decisions can be
executed reliably. Failure to find an edge must produce an explicit `NO EDGE` result rather
than another round of parameter tuning.

## Goals

- Start with gold, then add global equities and government bonds without changing the signal
  logic.
- Keep the complete decision path explainable as:
  `closed market data -> trend state -> target allocation -> risk checks -> order -> EUR result`.
- Use causal historical replay to obtain evidence quickly without exposing the bot to future
  data.
- Measure gross and net performance, including commission, spread, slippage, product expenses,
  financing/cash return where applicable, and currency conversion.
- Use one pre-registered hypothesis and one final untouched holdout test.
- Progress separately through research, prospective paper, EUR 200 micro-live operations, and
  economically meaningful live capital.
- Preserve a complete decision and execution journal so every action can be reconstructed.

## Non-goals

- Predict every movement or maximize the win rate.
- Intraday trading, latency-sensitive execution, scalping, market making, or order-flow models.
- Leverage, short selling, CFDs, futures, options, martingale, grid recovery, or averaging down.
- Live self-learning, autonomous parameter optimization, an LLM making trade decisions, or
  agents debating subjective market narratives.
- Choosing a proprietary trading firm during this project.
- Tax optimization or a guarantee that a specific product is suitable for the owner's personal
  circumstances.

## Anti-goals

The project is considered to have failed even if its backtest looks attractive when any of the
following is true:

- future information, revised data, or the final holdout influenced a decision;
- repeated tests on the holdout were used to select a rule;
- costs or EUR currency effects were hidden;
- profitability depends on one exceptional trade, one instrument, or one narrow date range;
- the implementation is too complex for a decision to be explained from its recorded inputs;
- paper or micro-live results are presented as proof of future profitability;
- the bot is changed merely to satisfy a proprietary firm's challenge rules.

## Constraints and owner decisions

- The owner resides in Portugal and wants withdrawals and account-level accounting understood
  in EUR.
- The intended broker is Interactive Brokers Ireland, subject to account eligibility and a
  final product/fee check before any broker integration is enabled.
- The first live amount is EUR 200. It is an operational micro-live test, not a profitability
  test and not the capital base assumed by the research.
- Positions may remain open for days, weeks, or months.
- Gold is the first research sleeve. Diversification follows only after the gold engine and
  validation path work correctly.
- The same fixed strategy logic must later apply to gold, global equities, and government
  bonds.
- Trading currency and economic currency exposure are different. A product traded in EUR can
  still contain USD or other currency exposure. All portfolio results and risks are converted
  to EUR explicitly.
- Existing real-money blocks remain in force until a separately approved live-execution design
  is completed.
- No paid data subscription or licence may be purchased without a separate owner approval that
  states the provider, price, licence constraints, and demonstrated gap in the free data path.

## Approaches considered

### 1. Gold-only trend/cash

This is the smallest possible research target and the fastest way to validate the causal
pipeline. It is easy to understand but concentrated, generates few independent observations,
and can spend long periods in a single market regime.

### 2. Gold first, then three sleeves using one engine — selected

Build and validate the engine with gold, then apply the identical rule to global equities and
government bonds. This adds diversification while retaining one decision model and one code
path. It is the selected approach because it balances simplicity with the need not to depend on
one asset.

### 3. Broad universe from the start

Testing many instruments would create more observations but would also multiply data mapping,
product selection, currency, cost, and multiple-testing problems. It conflicts with the goal
of reaching a trustworthy simple system quickly and is rejected for the first version.

## Economic hypothesis

The sole first-version hypothesis is:

> A slow, long-or-cash time-series trend rule applied identically to gold, global equities, and
> government bonds can produce positive EUR excess returns after realistic costs while reducing
> severe drawdowns relative to remaining continuously invested.

This is one portfolio hypothesis, not three opportunities to select only the winning asset.
Gold-only work validates the engine and reports a sleeve result; promotion to a diversified
strategy depends on the combined pre-registered portfolio.

## Trading rule

### Observation and schedule

- Use completed daily bars only.
- Evaluate signals once at each instrument's final trading session of the calendar month.
- Execute a changed target at the next tradable regular-session open. A final signal without a
  future open stays pending and is never filled synthetically.
- Run data and operational health checks daily even when no trade is due.

### Signal

For each sleeve, calculate the 10-month simple moving average from month-end total-return
levels available at that time:

- `LONG` when the latest completed month-end level is strictly above the 10-month average;
- `CASH` when it is at or below the average;
- `INSUFFICIENT_DATA` until ten completed month-end observations exist; this state never opens
  or closes a position.

There is one lookback, one comparison, and no score or confidence threshold. The 10-month
choice is pre-registered before the new experiment and is not tuned on the new holdout. A
provider's adjusted/total-return series may be used only when its point-in-time semantics are
documented; otherwise distributions must be modelled explicitly.

### Allocation

- Research phase 1 reports gold as a standalone long/cash sleeve.
- In the diversified portfolio, each of the three sleeves has a fixed one-third maximum weight.
- An active sleeve receives its one-third target; an inactive sleeve remains in EUR cash or an
  explicitly modelled EUR cash equivalent.
- There is no redistribution of an inactive sleeve to active sleeves, no leverage, and no
  volatility optimization in version 1.
- Rebalance only when the target changes or allocation drift exceeds a pre-registered execution
  tolerance selected from broker minimums before the holdout. The tolerance is an operational
  rule, not a backtested performance parameter.

### Instruments

Research symbols and executable products are separate concepts. Version 1 fixes these economic
exposures before any new result is viewed:

1. physical gold priced in USD and converted to EUR, without a currency hedge;
2. a broad global developed-and-emerging-markets equity total-return index, converted to EUR
   without a currency hedge;
3. a broad investment-grade euro-area government-bond total-return index, denominated in EUR
   across eligible maturities and therefore requiring no currency hedge.

The research may use durable index/proxy history to meet the 15-year minimum, but it may never
pretend that an index was directly tradable. A separate executable-product replay starts only at
the product's actual inception and includes its TER, tracking difference when available,
distributions, exchange calendar, spread, commissions, taxes represented in the published NAV,
and the product's next-open prices. Promotion requires that this shorter product-era replay is
directionally consistent with the proxy result and remains positive after realistic costs; it
is a fidelity check, not a second parameter-selection opportunity.

Before paper trading, each exposure is mapped to one unlevered European product available to
the owner's IBKR Ireland account. Equity and bond products should be UCITS funds where
applicable. Gold will normally require a physically backed ETC rather than a UCITS fund, so its
issuer, collateral, custody, KID, liquidity, spread, expense ratio, trading venue, and currency
exposure must be recorded. Product choice is an implementation input and may not be selected
because its past return made the strategy look better.

## What the bot knows

The runtime context is intentionally small and structured. For every decision it contains only:

- instrument identity and executable product mapping;
- completed bars known by the decision timestamp and their source/version;
- current 10-month average and resulting `LONG`, `CASH`, or `INSUFFICIENT_DATA` state;
- current position, target allocation, cash, and EUR conversion rates;
- estimated and realized commissions, spread, slippage, expenses, and currency conversion;
- data-quality state, broker reconciliation state, and safety state;
- the prior decision and whether it was executed, rejected, pending, or reconciled.

No prose news, discretionary narrative, LLM output, or future outcome enters the decision.
"Wisdom" accumulates as immutable experiment reports, decisions, costs, incidents, and
outcomes—not as hidden live adaptation. Any proposed rule change creates a new version and a
new research protocol; it never silently changes the running strategy.

## System structure

The new path should be isolated from the unsuccessful FX agents while reusing trustworthy
infrastructure only where that reduces, rather than hides, complexity.

```text
point-in-time data
        |
        v
monthly trend rule (pure)
        |
        v
fixed sleeve allocator (pure)
        |
        v
cost + EUR accounting simulator
        |
        +----> causal replay and sealed validation report
        |
        v
risk/health gates -> paper adapter -> later micro-live adapter
        |
        v
immutable decision, order, fill, reconciliation, and incident journal
```

The builder should prefer a small new package for this path over extending the five-agent FX
orchestrator. Existing data normalization, causal timing assertions, cost accounting patterns,
hash-chained journals, idempotent client order IDs, reconciliation, and safety blocks may be
reused behind narrow interfaces. Existing candle-pattern, price-action, ML, FX-agent voting,
and intraday modules are outside this decision path and need not be deleted.

## Causal replay and evidence protocol

### Data inventory before testing

Before results are calculated, produce and freeze a manifest containing series identifiers,
provider, retrieval timestamp, raw-file hashes, field semantics, calendars, timezone, currency,
distribution treatment, revisions, missing-data policy, and executable-product mapping. The
common history must include multiple bull, bear, inflation, rate, and crisis regimes. If it is
too short to support the fixed split below, the experiment stops rather than shortening the
holdout after seeing results.

### Chronological partitions

Use a fixed common-history split recorded in the experiment manifest before any result is
opened:

- first 60% of eligible completed months: development and engineering checks;
- next 20%: validation and confirmation of assumptions;
- final 20%: sealed holdout, run once by an explicit command that records its use.

Partition boundaries are rounded to month ends. At least 60 completed months must remain in the
sealed holdout and at least 180 completed months in the full common history. If these minima are
not available for all three sleeves, expand or replace the research data source; do not inspect
and resplit the holdout. Warm-up data before each partition may calculate the moving average but
may not contribute returns to that partition.

### Replay mechanics

- At replay time `t`, only records with an availability timestamp at or before `t` are visible.
- Decisions are made from the completed month and fills occur no earlier than the next valid
  open.
- Mutating or appending any data after `t` must leave all decisions, fills, and equity through
  `t` unchanged.
- Missing bars, stale EUR conversion rates, ambiguous calendars, or incomplete corporate-action
  handling fail closed.
- Every run stores the code version, configuration hash, data-manifest hash, results, and a
  human-readable trade-by-trade explanation.

### Cost cases

Report zero-cost diagnostics, expected realistic costs, and a stress case. The realistic case
uses documented broker/product inputs available before the holdout. The stress case doubles
commission, observed median spread, and slippage assumptions, while retaining product expenses
and EUR currency conversion. Cash earns the contemporaneous modelled EUR cash benchmark net of
any applicable costs; it is not assumed to return zero unless the data supports zero.

Flat commissions make results capital-dependent. Every report therefore runs the same frozen
decisions on an initial-capital grid of EUR 200, 1,000, 5,000, 10,000, 25,000, and 50,000. EUR
200 is labelled operational-only. The economically viable capital is the smallest grid value
for which all intended products can actually be sized, estimated annual trading and conversion
cost drag is no more than 0.50% of capital, and each ordinary one-way order costs no more than
0.10% of its notional. The promotion gates below are evaluated at that mechanically selected
capital, which may be larger than the owner is willing to fund; no result may imply that the
owner already possesses or should commit it.

`EUR cash benchmark` means the compounded daily euro short-term rate (€STR), after a fixed
0.25-percentage-point annual haircut, with negative rates retained. Portfolio idle cash is
modelled separately using the broker's documented interest tiers for each capital value; where
the broker would pay no interest, its return is zero. A money-market fund is not silently used
as cash because it would introduce another instrument, trade, and risk.

### Pre-registered promotion gates

The combined diversified portfolio is research-eligible for prospective paper only if the
sealed holdout, under realistic costs, meets all of these fixed conditions:

- positive cumulative EUR return and positive CAGR;
- CAGR exceeds the modelled EUR cash benchmark;
- annualized Sharpe ratio is at least 0.30, calculated from daily net EUR portfolio returns
  minus the same day's €STR benchmark return, using sample standard deviation and 252 sessions;
- maximum drawdown is no worse than 20%;
- the stress-cost result remains positive;
- at least three of four equal chronological holdout subperiods have non-negative net return;
- no single sleeve contributes more than 70% of the sum of positive net sleeve P&L
  contributions over the holdout; cash is excluded, and the gate fails as insufficient evidence
  if fewer than two sleeves have positive contributions;
- results and decisions reproduce exactly from the frozen manifest.

The report must also compare against continuously invested equal-weight exposure and disclose
turnover, exposure, longest drawdown, worst month, result by sleeve, top-trade concentration,
and uncertainty intervals. Passing these gates is evidence for proceeding, not proof or a
profit guarantee. Failure produces `NO STATISTICAL EDGE FOUND` or `INSUFFICIENT EVIDENCE`; the
holdout cannot be reused to repair version 1.

## Operational stages and gates

### Stage 0 — offline engineering

Verify formulas, timing, EUR conversion, costs, restart behavior, corrupt/missing data, and
idempotency with deterministic fixtures and property/causality tests. No broker connection is
needed.

Start with free or already licensed data. Before any market result is calculated, run a data
fitness audit against the manifest requirements. If the free path cannot establish
point-in-time/total-return semantics, sufficient history, EUR conversion, cash benchmark, or
reproducible raw files, stop and produce a provider comparison with exact current prices and
licence restrictions for owner approval. Never substitute a weaker series silently.

### Stage 1 — gold causal replay

Run gold through the complete version-1 path to prove the engine and expose concentration and
cost behavior, but use only the development and validation partitions. Gold's final 20% remains
sealed and is not reported or inspectable until the one combined holdout run in stage 2. This
stage cannot promote gold alone to live trading and cannot change the fixed rule based on
attractive results.

### Stage 2 — diversified sealed experiment

Add global equities and government bonds, freeze the manifest and protocol, complete
development/validation, then run the final holdout once. Only a passing combined report may
continue.

### Stage 3 — IBKR paper

Use the owner's IBKR Ireland paper account for at least six calendar months and six scheduled
month-end decision cycles. Paper validates data arrival, clock/calendar handling, order
construction, fractional-share/product constraints, restarts, reconciliation, alerts, and the
agreement between expected and observed costs. It does not independently prove profitability.
There must be no unresolved duplicate order, unprotected exposure, unexplained position,
currency-accounting break, or reconciliation incident before promotion.

### Stage 4 — EUR 200 micro-live

This is a separately approved real-money feature. The live adapter remains impossible to arm
until a future design covers credentials, endpoint/account identity, order types, manual
confirmation, kill switch, reconciliation, and recovery. The EUR 200 test performs the minimum
number of small buy/sell and cash/currency operations needed to verify the real path, records
the unavoidable cost drag, and then stops for review. Its return must never be used to estimate
strategy profitability.

### Stage 5 — economically meaningful live capital

No fixed capital amount is promised in this design. Before funding, calculate the minimum from
actual product eligibility, fractional dealing, commissions, spreads, currency conversion,
expected annual turnover, and acceptable annual cost drag. Increase capital only after the
paper and micro-live incident reports are clean and the owner explicitly approves a separate
live risk policy. Scaling is gradual and can stop or reverse.

### Stage 6 — optional proprietary-firm assessment

Proprietary firms are a possible capital-scaling channel only after the strategy and live path
are already satisfactory. Evaluate current legal/entity status, whether trading is simulated or
funded, automation/API permission, permitted products, holding overnight/weekend, news rules,
daily and trailing drawdown formulas, consistency rules, fees, payout record, platform, data,
and tax treatment. Reject any firm whose rules are incompatible with slow monthly trend
positions or require changing the economic strategy merely to pass a challenge.

## Risk and safety

- Long/cash only, no leverage and no borrowing in version 1.
- A sleeve cannot exceed one-third in the diversified portfolio.
- An order is blocked on stale/missing data, stale FX conversion, unknown account identity,
  unexpected holdings, unreconciled prior order, excessive spread versus the frozen operational
  limit, or engaged kill switch.
- Operational kill switches block new entries and allow only explicitly reconciled risk-reducing
  exits; they do not pretend to cap market losses at an exact percentage.
- Market drawdown alerts are reporting gates, not intramonth discretionary exits. A proposed
  drawdown stop is a new strategy hypothesis and cannot be added silently.
- The system never sends an automatic order after an ambiguous response. It reconciles the
  deterministic order ID first.
- Paper and live state are physically and visibly separated. Live remains disabled by default
  and requires explicit arming on every start.

## Reporting and user experience

The primary output is a compact Portuguese report, not an agent control room. For each scheduled
decision it shows:

- data timestamp and health;
- each sleeve's level, 10-month average, state, current allocation, and target allocation;
- orders needed, estimated EUR costs, and reason for trade or no trade;
- account value, cash, exposure, cumulative net result, drawdown, and benchmark comparison;
- broker/paper environment, reconciliation state, and any blocking safety reason.

Every trade explanation must fit the same template: "At month-end X, completed data put asset Y
above/below its 10-month average; target changed from A to B; after health and cost checks the
order was executed/rejected/pending at Z; resulting EUR costs were C."

## Testing and acceptance

Implementation acceptance requires, at minimum:

- unit tests for month-end sampling, moving-average warm-up and equality boundary, allocation,
  EUR conversion, cost accounting, and state transitions;
- causality tests that mutate all future data without changing prior results;
- next-open/calendar tests across weekends, holidays, daylight-saving changes, and different
  instrument venues;
- adjusted-series/distribution tests and missing/revised-data failure cases;
- portfolio accounting identities and independent hand-calculated fixtures;
- restart, duplicate-order, ambiguous response, partial fill, and reconciliation tests before
  any broker order capability;
- deterministic reproduction from the frozen data and configuration hashes;
- static checks and the relevant unit/integration suite passing with no unexplained failures.

## Delivery boundaries

This design authorizes planning only after owner approval. Implementation should be delivered in
small reviewable phases and must not remove the current research or FX code. The first delivery
ends with an offline gold engine and causal tests; broker integration and real-money enablement
are later, separately approved deliveries.

The builder may choose internal class names, file layout inside the new package, serialization
format compatible with existing project conventions, and presentation details that do not
change the economic rule, evidence protocol, safety boundaries, or promotion gates.
