"""Timezone / session normalization for daily bars.

Providers label daily bars differently (Yahoo: local midnight of the session date). We map
every daily bar to its exchange session and re-stamp it with the session OPEN in UTC, which is
the platform-wide convention for `price_bars.ts`. Nothing is altered besides the timestamp.
"""

from dataclasses import dataclass, field
from datetime import time

import pandas as pd

from app.data.calendar import TradingCalendar
from app.data.providers.base import BAR_COLUMNS, ProviderBars, ProviderDataError
from app.data.quality import EXCLUDED, KEPT_FLAGGED, DataQualityIssue


@dataclass
class NormalizedBars:
    frame: pd.DataFrame  # index: ts (session open, UTC); columns: BAR_COLUMNS + session_date, close_utc
    sessions: pd.DataFrame  # calendar sessions covering the provider's date span
    issues: list[DataQualityIssue] = field(default_factory=list)


def normalize_daily(bars: ProviderBars, calendar: TradingCalendar) -> NormalizedBars:
    raw = bars.frame
    if not isinstance(raw.index, pd.DatetimeIndex) or raw.index.tz is None:
        raise ProviderDataError("bars must have a timezone-aware DatetimeIndex")

    issues: list[DataQualityIssue] = []
    columns = [*BAR_COLUMNS, "session_date", "close_utc"]
    if raw.empty:
        empty = pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], tz="UTC", name="ts"))
        return NormalizedBars(empty, calendar.sessions(calendar.last_session, calendar.first_session))

    local = raw.index.tz_convert(calendar.tz)
    session_dates = pd.DatetimeIndex(local.tz_localize(None).normalize())

    for provider_ts, local_ts in zip(raw.index, local, strict=True):
        if local_ts.time() != time(0, 0):
            issues.append(
                DataQualityIssue(
                    check_name="unexpected_daily_label_time",
                    severity="warning",
                    description="Daily bar not labelled at local midnight; mapped by local date.",
                    action_taken=KEPT_FLAGGED,
                    details={"provider_ts": provider_ts.isoformat(), "exchange_tz": calendar.tz},
                )
            )

    sessions = calendar.sessions(session_dates.min().date(), session_dates.max().date())
    is_session = session_dates.isin(sessions.index)

    for provider_ts, d in zip(raw.index[~is_session], session_dates[~is_session], strict=True):
        issues.append(
            DataQualityIssue(
                check_name="non_session_date",
                severity="error",
                description=f"Bar dated {d.date()} but {calendar.exchange} has no session that day.",
                action_taken=EXCLUDED,
                details={"provider_ts": provider_ts.isoformat(), "session_date": d.date().isoformat()},
            )
        )

    kept = raw[is_session].copy()
    kept_dates = session_dates[is_session]
    matched = sessions.reindex(kept_dates)
    kept["session_date"] = [d.date() for d in kept_dates]
    kept["close_utc"] = matched["close_utc"].array
    kept.index = pd.DatetimeIndex(matched["open_utc"].array, name="ts")
    return NormalizedBars(kept[columns], sessions, issues)
