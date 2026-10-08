"""XNYS session logic for operations (exchange calendar only; no local-timezone assumptions).

A session is ELIGIBLE once `close_utc + grace` has passed, where `close_utc` is the official
regular-session close from the calendar (early closes included). Weekends and holidays are not
sessions. "Previous session" always means the previous calendar session, never yesterday.
"""

from datetime import date, datetime, timedelta

import pandas as pd

from app.data.calendar import TradingCalendar

LOOKBACK_DAYS = 400  # far enough back to find the sessions around any downtime


def _utc(now: datetime) -> pd.Timestamp:
    ts = pd.Timestamp(now)
    if ts.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return ts.tz_convert("UTC")


def schedule(cal: TradingCalendar, start: date, end: date) -> pd.DataFrame:
    return cal.sessions(start, end)


def latest_eligible_session(cal: TradingCalendar, now: datetime, grace_minutes: int) -> date | None:
    """Latest session whose close + grace is at or before `now`."""
    t = _utc(now)
    s = cal.sessions(t.date() - timedelta(days=15), t.date() + timedelta(days=1))
    done = s[s["close_utc"] + pd.Timedelta(minutes=grace_minutes) <= t]
    return None if done.empty else pd.Timestamp(done.index[-1]).date()


def sessions_after(cal: TradingCalendar, after: date | None, upto: date) -> list[date]:
    """Calendar sessions in (after, upto], oldest first."""
    start = (after + timedelta(days=1)) if after else upto - timedelta(days=LOOKBACK_DAYS)
    if start > upto:
        return []
    return [pd.Timestamp(d).date() for d in cal.sessions(start, upto).index]


def previous_session(cal: TradingCalendar, day: date) -> date | None:
    s = cal.sessions(day - timedelta(days=15), day - timedelta(days=1))
    return None if s.empty else pd.Timestamp(s.index[-1]).date()


def session_times(cal: TradingCalendar, day: date) -> tuple[pd.Timestamp, pd.Timestamp]:
    s = cal.sessions(day, day)
    if s.empty:
        raise ValueError(f"{day} is not an {cal.exchange} session")
    return pd.Timestamp(s["open_utc"].iloc[0]), pd.Timestamp(s["close_utc"].iloc[0])


def eligible_at(cal: TradingCalendar, day: date, grace_minutes: int) -> pd.Timestamp:
    return session_times(cal, day)[1] + pd.Timedelta(minutes=grace_minutes)


def next_eligibility(cal: TradingCalendar, now: datetime, grace_minutes: int) -> pd.Timestamp:
    """The next time (> now) at which a new session becomes eligible."""
    t = _utc(now)
    s = cal.sessions(t.date() - timedelta(days=1), t.date() + timedelta(days=20))
    times = s["close_utc"] + pd.Timedelta(minutes=grace_minutes)
    later = times[times > t]
    return pd.Timestamp(later.iloc[0])
