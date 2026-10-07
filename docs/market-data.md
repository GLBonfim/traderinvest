# Market Data (Phase 2)

Pipeline: `DataProvider.fetch_bars` → `normalize_daily` → `validate_daily` → persist → record
issues. Entry point: `uv run python -m app.data.cli ingest --symbol SPY --start 1993-01-01`.

## Scope

| Item | Status |
|---|---|
| Instrument | SPY (underlying S&P 500, listed ARCX, calendar XNYS) |
| Provider | yfinance (prototype only) |
| Timeframe | 1d |
| Intraday / 4H | Not implemented (methodology fixed in ADR-0005) |

## Provider contract (`app/data/providers/base.py`)

`fetch_bars(symbol, timeframe, start, end)` — `end` inclusive — returns `ProviderBars` with
columns `open, high, low, close, adj_close, volume, split_ratio`, a tz-aware index exactly as the
provider labels it, `provider_version` and `fetched_at`. Providers must not normalize, validate
or correct. Failures raise `ProviderUnavailableError`; contract violations (missing columns,
naive timestamps) raise `ProviderDataError`.

Adding a provider (e.g. Massive): implement `DataProvider`, add its ticker to the instrument's
`provider_symbols`, register it in `app/data/cli.py`. Nothing else changes; bars are stored per
provider, so sources can be compared.

## Price semantics (yfinance)

Called with `auto_adjust=False, back_adjust=False, repair=False, actions=True`.

| Column | Meaning |
|---|---|
| `open/high/low/close` | Yahoo values: **split-adjusted, not dividend-adjusted**. Equal to as-traded prices when no later split exists. SPY has had no splits. |
| `adj_close` | Split- and dividend-adjusted (total-return) close. Yahoo restates history after each dividend. |
| `volume` | Shares, as reported. |

Use `close` for price levels/structure and `adj_close` for return studies.

## Timezone and session normalization

- All stored timestamps are UTC.
- Daily bar `ts` = **session open in UTC** from the XNYS calendar (e.g. 2024-07-01 → 13:30 UTC,
  2024-03-08 → 14:30 UTC because of DST).
- Yahoo labels daily bars at local midnight; the bar is mapped by its **exchange-local date**.
  A label that is not local midnight is flagged (`unexpected_daily_label_time`) but still mapped.
- A bar whose local date is not an XNYS session is excluded (`non_session_date`).
- `market_sessions` stores open/close/early-close for every session in the requested range.

## Closed vs forming bars

`is_closed = session close (UTC) <= as_of`. `as_of` defaults to the run time and is stored on
the run. A forming bar is stored with `is_closed = false` and replaced on a later run without a
revision warning. Bars whose session opens after `as_of` are excluded (`future_bar`).

## Validation (`app/data/validation.py`)

| Check | Severity | Action |
|---|---|---|
| `no_data` | error | recorded_only |
| `non_session_date` | error | excluded |
| `conflicting_duplicate_bar` | error | excluded (all copies) |
| `duplicate_bar` (identical) | warning | deduplicated_identical |
| `null_value` (OHLC/volume) | error | excluded |
| `non_positive_price`, `non_positive_adj_close` | error | excluded |
| `ohlc_inconsistent` (tolerance 1e-6) | error | excluded |
| `negative_volume` | error | excluded |
| `future_bar` | error | excluded |
| `zero_volume` | warning | kept_flagged |
| `missing_adj_close` | warning | kept_flagged |
| `split_detected` | warning | kept_flagged |
| `return_outlier` (\|close/prev close − 1\| > 10%) | warning | kept_flagged |
| `gap_outlier` (\|open/prev close − 1\| > 7%) | warning | kept_flagged |
| `adj_factor_decrease` (adj_close/close falls > 1e-4) | warning | kept_flagged |
| `missing_session` | warning | recorded_only |

`missing_session` covers sessions from the first received bar up to the requested end that have
closed by `as_of`. Sessions before the first bar are not reported (the instrument may not have
existed; SPY listed 1993-01-29). Excluded bars are not double-reported as missing.

Outliers are **never removed**: they flag genuine market events (2008, 2020) as well as errors.

## Persistence and idempotency (`app/data/ingestion.py`)

Bars are keyed by `(instrument_id, provider, timeframe, ts)`.

| Situation | Result |
|---|---|
| New bar | inserted |
| Identical bar | unchanged |
| Forming bar changed / closed | updated, no event |
| Raw OHLCV of a closed bar changed | updated + `bar_revised` warning (old/new values) |
| Only `adj_close` changed by > 1e-5 relative | updated + one `adj_close_restated` info event per run (count, range, max change) |
| `adj_close` changed by ≤ 1e-5 relative | **stored value kept**, counted as `adj_close_noise_ignored` in the run log |

Why the tolerance: consecutive Yahoo requests return `adj_close` values differing by up to
~1.4e-6 relative (observed 2026-10-07, ~7,000 SPY bars per request). A real SPY dividend
restatement is ~1e-3. Without the tolerance every run rewrote ~7,000 rows and the real
restatements were indistinguishable from noise.

Every run creates an `ingestion_runs` row (request, `as_of`, provider version, counts, status,
error). Every issue becomes a `data_quality_events` row linked to its run. Events are a per-run
audit log, so recurring flags (e.g. the same outliers) are recorded again on each run. A
provider failure marks the run `failed`, writes no bars and records a `provider_failure`
critical event.

## First real ingestion (2026-10-07, yfinance 1.7.0)

| Metric | Value |
|---|---|
| Bars | 8,479 (1993-01-29 → 2026-10-06), all closed |
| Excluded | 0 |
| Missing sessions | 0 |
| Flagged (kept) | 8: gaps 2001-09-17, 2008-10-24, 2020-03-09, 2020-03-16; returns 2008-10-13, 2008-10-28, 2020-03-16, 2025-04-09 |
| Repeat runs | 0 inserted, 0 updated, 8,479 unchanged |

These are data-quality facts only, not analysis.

## Limitations

- yfinance is unofficial and may change or break; values can differ from official sources.
- Pre-split OHLC from Yahoo are split-adjusted, not as-traded (irrelevant for SPY so far).
- Dividends and splits are not stored as a separate corporate-actions table; dividends are only
  reflected through `adj_close`.
- No cross-provider reconciliation until a second provider exists.
