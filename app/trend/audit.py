"""Data-fitness audit (phase T1): quality checks only.

Nothing here computes a moving average, a signal, a strategy return or a benchmark result.
The only per-day arithmetic is what data quality requires: total-return factors to verify the
provider's dividend adjustment and to flag implausible jumps.
"""

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.trend.loaders import total_return_factors

EXTREME_MOVE = 0.15  # |daily total return| above this is listed for manual review
ADJ_TOLERANCE = 1e-4  # max |reconstructed - provider| daily return accepted per session
MAX_FX_STALENESS = 4  # weekdays between an asset session and the latest ECB rate
WARMUP_SESSIONS = 200
MIN_EVALUABLE_YEARS = 15.0


def calendar_sessions(name: str, start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    import exchange_calendars as xcals

    cal = xcals.get_calendar(name, start=max(start, pd.Timestamp("1990-01-01")))
    sessions = cal.sessions_in_range(start, end)
    return pd.DatetimeIndex(sessions.tz_localize(None) if sessions.tz else sessions)


def longest_constant_run(values: pd.Series) -> tuple[int, str | None]:
    """Length (in rows) and end date of the longest run of identical consecutive values."""
    if values.empty:
        return 0, None
    same = values.eq(values.shift(1)).to_numpy()
    best, best_end, run = 1, values.index[0], 1
    for i in range(1, len(values)):
        run = run + 1 if same[i] else 1
        if run > best:
            best, best_end = run, values.index[i]
    return best, pd.Timestamp(best_end).date().isoformat()


def _dates(index: pd.Index, limit: int = 20) -> list[str]:
    return [pd.Timestamp(d).date().isoformat() for d in list(index)[:limit]]


def audit_prices(frame: pd.DataFrame, calendar: str) -> dict[str, Any]:
    """Quality report for one stored price series (already cut at the data end)."""
    idx = pd.DatetimeIndex(frame.index)
    report: dict[str, Any] = {
        "rows": len(frame),
        "first": idx[0].date().isoformat(),
        "last": idx[-1].date().isoformat(),
        "duplicates": int(idx.duplicated().sum()),
        "monotonic": bool(idx.is_monotonic_increasing),
        "nan_close": int(frame["close"].isna().sum()),
        "nan_adj_close": int(frame["adj_close"].isna().sum()),
        "non_positive_close": int((frame["close"] <= 0).sum()),
        "zero_volume_rows": int((frame["volume"].fillna(0) == 0).sum()),
        "dividend_rows": int((frame["dividends"].fillna(0) != 0).sum()),
        "split_rows": int((frame["splits"].fillna(0) != 0).sum()),
    }
    sessions = calendar_sessions(calendar, idx[0], idx[-1])
    missing = sessions.difference(idx)
    extra = idx.difference(sessions)
    report["calendar"] = calendar
    report["calendar_sessions"] = len(sessions)
    report["missing_sessions"] = len(missing)
    report["missing_sessions_sample"] = _dates(missing)
    report["extra_rows"] = len(extra)
    report["extra_rows_sample"] = _dates(extra)
    run, run_end = longest_constant_run(frame["close"])
    report["longest_constant_close_run"] = run
    report["longest_constant_close_run_end"] = run_end

    # Data check: the provider's Adj Close must follow from Close and Dividends under the
    # provider's own method, close_t / (close_{t-1} - dividend_t).
    provider = frame["adj_close"] / frame["adj_close"].shift(1) - 1.0
    provider_method = frame["close"] / (frame["close"].shift(1) - frame["dividends"]) - 1.0
    diff = (provider_method - provider).abs().dropna()
    report["adjustment_max_abs_diff"] = float(diff.max()) if len(diff) else 0.0
    report["adjustment_sessions_above_tolerance"] = int((diff > ADJ_TOLERANCE).sum())
    report["adjustment_worst_dates"] = _dates(diff.sort_values(ascending=False).index[:5], 5)
    # Convention gap (informative): the research convention (close + dividend) / previous close
    # differs from the provider's on ex-dates only.
    recon = total_return_factors(frame["close"], frame["dividends"]) - 1.0
    convention = (recon - provider).abs().dropna()
    report["convention_max_abs_diff"] = float(convention.max()) if len(convention) else 0.0
    extreme = recon[recon.abs() > EXTREME_MOVE]
    report["extreme_moves"] = [
        {"date": d.date().isoformat(), "tr_return": round(float(v), 4)}
        for d, v in zip(pd.DatetimeIndex(extreme.index), extreme.to_numpy(), strict=True)
    ]
    return report


def fx_staleness(fx: pd.Series, dates: pd.DatetimeIndex) -> pd.Series:
    """Weekdays between each date and the latest ECB rate dated on or before it."""
    rate_dates = pd.DatetimeIndex(fx.dropna().index)
    pos = rate_dates.searchsorted(dates, side="right") - 1
    out = np.full(len(dates), -1, dtype=np.int64)
    ok = pos >= 0
    if ok.any():
        latest = rate_dates[pos[ok]]
        out[ok] = np.busday_count(
            latest.values.astype("datetime64[D]"), dates[ok].values.astype("datetime64[D]")
        )
    return pd.Series(out, index=dates, name="fx_staleness_weekdays")


def audit_fx(fx: pd.Series, dates: pd.DatetimeIndex) -> dict[str, Any]:
    stale = fx_staleness(fx, dates)
    return {
        "first": pd.Timestamp(fx.index[0]).date().isoformat(),
        "last": pd.Timestamp(fx.index[-1]).date().isoformat(),
        "observations": len(fx),
        "non_positive": int((fx <= 0).sum()),
        "sessions_checked": len(dates),
        "sessions_without_prior_rate": int((stale < 0).sum()),
        "max_staleness_weekdays": int(stale.max()) if len(stale) else 0,
        "sessions_over_limit": int((stale > MAX_FX_STALENESS).sum()),
        "limit_weekdays": MAX_FX_STALENESS,
    }


def audit_rates(
    eonia: pd.Series, estr: pd.Series, splice: pd.Timestamp, start: pd.Timestamp
) -> dict[str, Any]:
    """Coverage of the cash-rate chain EONIA (until `splice`) then EUR STR."""
    old = eonia.loc[(eonia.index >= start) & (eonia.index < splice)]
    new = estr.loc[estr.index >= splice]
    chained = pd.concat([old, new])
    gaps = np.busday_count(
        chained.index[:-1].values.astype("datetime64[D]"),
        chained.index[1:].values.astype("datetime64[D]"),
    )
    return {
        "eonia_first_used": pd.Timestamp(old.index[0]).date().isoformat() if len(old) else None,
        "eonia_last_used": pd.Timestamp(old.index[-1]).date().isoformat() if len(old) else None,
        "estr_first": pd.Timestamp(new.index[0]).date().isoformat() if len(new) else None,
        "estr_last": pd.Timestamp(new.index[-1]).date().isoformat() if len(new) else None,
        "max_gap_weekdays": int(gaps.max()) if len(gaps) else 0,
        "nan": int(chained.isna().sum()),
    }


@dataclass(frozen=True)
class Window:
    warmup_end: dict[str, str]  # asset -> date of its 200th session
    evaluable_start: str
    data_end: str
    years: float
    binding_asset: str


def evaluable_window(
    sessions: dict[str, pd.DatetimeIndex],
    extra_starts: dict[str, pd.Timestamp],
    master: pd.DatetimeIndex,
    data_end: pd.Timestamp,
    warmup: int = WARMUP_SESSIONS,
) -> Window:
    """First master session at which every asset has `warmup` closes and every benchmark-only
    series has started. Raises if any asset lacks enough sessions."""
    ready: dict[str, pd.Timestamp] = {}
    for name, idx in sessions.items():
        if len(idx) < warmup:
            raise ValueError(f"{name}: only {len(idx)} sessions, needs {warmup}")
        ready[name] = pd.Timestamp(idx[warmup - 1])
    candidates = {**ready, **{k: pd.Timestamp(v) for k, v in extra_starts.items()}}
    binding = max(candidates, key=lambda k: candidates[k])
    first_ok = master[master >= candidates[binding]]
    if first_ok.empty:
        raise ValueError("no master session after warm-up")
    start = pd.Timestamp(first_ok[0])
    years = (data_end - start).days / 365.25
    return Window(
        warmup_end={k: v.date().isoformat() for k, v in ready.items()},
        evaluable_start=start.date().isoformat(),
        data_end=data_end.date().isoformat(),
        years=round(years, 3),
        binding_asset=binding,
    )


def partition_bounds(
    master: pd.DatetimeIndex, start: pd.Timestamp, end: pd.Timestamp
) -> dict[str, dict[str, str]]:
    """60/20/20 chronological split of master sessions in [start, end], cut at month ends.

    Each cut is the last session of a calendar month on or before the 60 % (resp. 80 %) session.
    """
    span = master[(master >= start) & (master <= end)]
    if len(span) < 10:
        raise ValueError("too few sessions to partition")
    month_end = span.to_series().groupby(span.to_period("M")).max()
    month_ends = pd.DatetimeIndex(month_end.to_numpy())

    def cut(fraction: float) -> pd.Timestamp:
        target = span[int(np.floor(fraction * len(span))) - 1]
        eligible = month_ends[month_ends <= target]
        if eligible.empty:
            raise ValueError("no month end before the cut")
        return pd.Timestamp(eligible[-1])

    dev_end, val_end = cut(0.60), cut(0.80)

    def after(ts: pd.Timestamp) -> pd.Timestamp:
        return pd.Timestamp(span[span > ts][0])

    def iso(ts: pd.Timestamp) -> str:
        return ts.date().isoformat()

    return {
        "development": {"start": iso(pd.Timestamp(span[0])), "end": iso(dev_end)},
        "validation": {"start": iso(after(dev_end)), "end": iso(val_end)},
        "sealed": {"start": iso(after(val_end)), "end": iso(pd.Timestamp(span[-1]))},
    }
