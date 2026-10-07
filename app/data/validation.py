"""Data-quality validation for normalized daily bars.

Policy: never fix data silently. Each check emits `DataQualityIssue`s and either
EXCLUDES the bar (structurally invalid: it cannot be trusted at all) or KEEPS it flagged
(plausible but unusual: real market events such as crashes must not be removed).
"""

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd

from app.data.quality import (
    DEDUPLICATED,
    EXCLUDED,
    KEPT_FLAGGED,
    RECORDED_ONLY,
    DataQualityIssue,
    Severity,
)

PRICE_COLUMNS = ("open", "high", "low", "close")
REQUIRED_COLUMNS = (*PRICE_COLUMNS, "volume")


@dataclass(frozen=True)
class ValidationConfig:
    # Flag (not remove) close-to-close moves larger than this. SPY's largest daily moves
    # (2008, 2020) are ~10-15%, so these are expected to fire on genuine events.
    max_abs_return: float = 0.10
    # Flag (not remove) open-vs-previous-close gaps larger than this.
    max_abs_gap: float = 0.07
    # Absolute tolerance for OHLC consistency (provider floats carry ~1e-6 rounding).
    ohlc_tolerance: float = 1e-6
    # Relative tolerance for the dividend adjustment factor (adj_close / close) going backwards.
    adj_factor_tolerance: float = 1e-4


@dataclass
class ValidationResult:
    valid: pd.DataFrame  # bars allowed to be persisted, with `is_closed`
    issues: list[DataQualityIssue] = field(default_factory=list)
    excluded_count: int = 0


def _issue(
    check: str,
    severity: Severity,
    description: str,
    action: str,
    ts: pd.Timestamp | None = None,
    **details: object,
) -> DataQualityIssue:
    return DataQualityIssue(
        check_name=check,
        severity=severity,
        description=description,
        action_taken=action,
        bar_ts=ts.to_pydatetime() if ts is not None else None,
        details={k: _jsonable(v) for k, v in details.items()},
    )


def _jsonable(v: object) -> object:
    if isinstance(v, float | np.floating):
        return None if np.isnan(v) else float(v)
    if isinstance(v, date | datetime | pd.Timestamp):
        return v.isoformat()
    if isinstance(v, np.integer):
        return int(v)
    return v


def validate_daily(
    frame: pd.DataFrame,
    sessions: pd.DataFrame,
    *,
    as_of: datetime,
    requested_end: date,
    config: ValidationConfig | None = None,
) -> ValidationResult:
    cfg = config or ValidationConfig()
    issues: list[DataQualityIssue] = []
    as_of_ts = pd.Timestamp(as_of)
    if as_of_ts.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")

    if frame.empty:
        issues.append(_issue("no_data", "error", "Provider returned no bars.", RECORDED_ONLY))
        return ValidationResult(frame.assign(is_closed=pd.Series(dtype=bool)), issues, 0)

    df = frame.sort_index(kind="stable")
    bad = np.zeros(len(df), dtype=bool)

    # 1. Duplicate session timestamps.
    dup_mask = df.index.duplicated(keep=False)
    if dup_mask.any():
        for ts in df.index[dup_mask].unique():
            rows = df.loc[[ts]]
            identical = len(rows.drop_duplicates()) == 1
            idx = np.flatnonzero(df.index == ts)
            if identical:
                bad[idx[1:]] = True
                issues.append(
                    _issue("duplicate_bar", "warning", "Identical duplicate bars; one kept.",
                           DEDUPLICATED, ts, copies=len(rows))
                )
            else:
                bad[idx] = True
                issues.append(
                    _issue("conflicting_duplicate_bar", "error",
                           "Duplicate bars with different values; all copies excluded.",
                           EXCLUDED, ts, copies=len(rows))
                )

    def exclude(mask: np.ndarray, check: str, desc: str) -> None:
        for i in np.flatnonzero(mask & ~bad):
            row = df.iloc[int(i)]
            issues.append(
                _issue(check, "error", desc, EXCLUDED, pd.Timestamp(df.index[int(i)]),
                       **{c: row[c] for c in (*PRICE_COLUMNS, "adj_close", "volume")})
            )
        np.logical_or(bad, mask, out=bad)

    # 2. Missing required values.
    exclude(df[list(REQUIRED_COLUMNS)].isna().any(axis=1).to_numpy(), "null_value",
            "Missing open/high/low/close/volume.")
    # 3. Non-positive prices.
    exclude((df[list(PRICE_COLUMNS)] <= 0).any(axis=1).to_numpy(), "non_positive_price",
            "Price <= 0.")
    exclude((df["adj_close"] <= 0).to_numpy(), "non_positive_adj_close", "Adjusted close <= 0.")
    # 4. OHLC consistency.
    tol = cfg.ohlc_tolerance
    hi_bad = df["high"] + tol < df[["open", "close", "low"]].max(axis=1)
    lo_bad = df["low"] - tol > df[["open", "close", "high"]].min(axis=1)
    exclude((hi_bad | lo_bad).to_numpy(), "ohlc_inconsistent",
            "High below open/close/low or low above open/close/high.")
    # 5. Negative volume.
    exclude((df["volume"] < 0).to_numpy(), "negative_volume", "Volume < 0.")
    # 6. Bars from the future relative to the decision time (look-ahead guard).
    exclude(np.asarray(df.index > as_of_ts), "future_bar",
            "Bar session opens after as_of; cannot exist yet.")

    valid = df[~bad].copy()
    excluded = int(bad.sum())

    # ── Checks below keep the bar and only flag it. ──
    for ts in valid.index[(valid["volume"] == 0).to_numpy()]:
        issues.append(_issue("zero_volume", "warning", "Volume is zero.", KEPT_FLAGGED, ts))

    for ts in valid.index[valid["adj_close"].isna().to_numpy()]:
        issues.append(
            _issue("missing_adj_close", "warning", "Adjusted close missing.", KEPT_FLAGGED, ts,
                   close=valid.at[ts, "close"])
        )

    for ts in valid.index[(valid["split_ratio"].fillna(0) != 0).to_numpy()]:
        issues.append(
            _issue("split_detected", "warning",
                   "Split reported. Provider prices before this date are split-adjusted, "
                   "not as-traded.", KEPT_FLAGGED, ts, split_ratio=valid.at[ts, "split_ratio"])
        )

    prev_close = valid["close"].shift(1)
    ret = valid["close"] / prev_close - 1
    gap = valid["open"] / prev_close - 1
    for ts in valid.index[(ret.abs() > cfg.max_abs_return).to_numpy()]:
        issues.append(
            _issue("return_outlier", "warning",
                   f"|close-to-close return| > {cfg.max_abs_return:.0%}.", KEPT_FLAGGED, ts,
                   ret=ret.loc[ts], prev_close=prev_close.loc[ts], close=valid.at[ts, "close"])
        )
    for ts in valid.index[(gap.abs() > cfg.max_abs_gap).to_numpy()]:
        issues.append(
            _issue("gap_outlier", "warning", f"|open gap vs previous close| > {cfg.max_abs_gap:.0%}.",
                   KEPT_FLAGGED, ts, gap=gap.loc[ts], prev_close=prev_close.loc[ts],
                   open=valid.at[ts, "open"])
        )

    # Dividend adjustment factor must be non-decreasing forward in time.
    factor = valid["adj_close"] / valid["close"]
    factor_change = factor / factor.shift(1) - 1
    for ts in valid.index[(factor_change < -cfg.adj_factor_tolerance).to_numpy()]:
        issues.append(
            _issue("adj_factor_decrease", "warning",
                   "adj_close/close factor decreased; inconsistent adjustment.", KEPT_FLAGGED, ts,
                   factor=factor.loc[ts], previous_factor=factor.shift(1).loc[ts])
        )

    # Missing sessions between the first bar and the last CLOSED session we could expect.
    if not valid.empty:
        first = valid["session_date"].min()
        expected = sessions[
            (sessions.index >= pd.Timestamp(first))
            & (sessions.index <= pd.Timestamp(requested_end))
            & (sessions["close_utc"] <= as_of_ts)
        ]
        have = set(valid["session_date"])
        # Excluded bars are already reported; do not double-report them as missing.
        have |= set(df["session_date"])
        for d, open_utc in zip(expected.index, expected["open_utc"], strict=True):
            if d.date() not in have:
                issues.append(
                    _issue("missing_session", "warning",
                           "Exchange session has no bar from the provider.", RECORDED_ONLY,
                           open_utc, session_date=d.date())
                )

    valid["is_closed"] = valid["close_utc"] <= as_of_ts
    return ValidationResult(valid, issues, excluded)
