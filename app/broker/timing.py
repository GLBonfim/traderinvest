"""OPG submission window (ADR-0026). Pure functions: no network, no wall clock.

Alpaca documentation (orders-at-alpaca, verified 2026-10-10): "OPG orders submitted after 9:28am
but before 7:00pm ET will be rejected"; "OPG orders submitted after 7:00pm will be queued and
routed to the following day's opening auction"; unfilled OPG orders are cancelled after the open.

An `opg` order for the opening auction of session S may therefore be sent only:
  - at or after 19:00 ET of the XNYS session before S (earlier, it would reach an earlier
    auction or Alpaca's rejection window), and
  - before 09:28 ET of S, and
  - never inside the daily 09:28-19:00 ET band. The documentation does not say whether that band
    also applies on weekends and holidays, so it is applied on EVERY calendar day (conservative:
    the worst case skips a time Alpaca would have accepted).
Each boundary is tightened by `safety_seconds` (clock skew, request latency). Times are computed
in America/New_York with zoneinfo, so DST transitions are handled; sessions come from the XNYS
exchange calendar, so weekends and holidays are handled.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from app.data.calendar import TradingCalendar

NEW_YORK = ZoneInfo("America/New_York")
OPG_CUTOFF = time(9, 28)  # submissions after this are rejected...
OPG_QUEUE_OPENS = time(19, 0)  # ...until this; afterwards they queue for the next auction


@dataclass(frozen=True)
class OpgWindow:
    session: date  # the auction's session
    previous_session: date
    opens_at: datetime  # UTC, inclusive (19:00 ET of the previous session + safety)
    closes_at: datetime  # UTC, exclusive (09:28 ET of the session - safety)


def _utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return t.astimezone(UTC)


def opg_window(cal: TradingCalendar, scheduled_open: datetime, safety_seconds: float) -> OpgWindow:
    """The submission window for the opening auction that starts at `scheduled_open`.
    Raises ValueError if `scheduled_open` is not the open of an XNYS session."""
    open_utc = pd.Timestamp(_utc(scheduled_open))
    day = open_utc.tz_convert(NEW_YORK).date()
    s = cal.sessions(day - timedelta(days=15), day)
    if s.empty or pd.Timestamp(s.index[-1]).date() != day:
        raise ValueError(f"{day} is not an {cal.exchange} session")
    if pd.Timestamp(s["open_utc"].iloc[-1]) != open_utc:
        raise ValueError(f"{open_utc.isoformat()} is not the open of session {day}")
    if len(s) < 2:
        raise ValueError(f"no session before {day} in the calendar window")
    prev = pd.Timestamp(s.index[-2]).date()
    margin = timedelta(seconds=safety_seconds)
    opens = datetime.combine(prev, OPG_QUEUE_OPENS, NEW_YORK) + margin
    closes = datetime.combine(day, OPG_CUTOFF, NEW_YORK) - margin
    return OpgWindow(day, prev, opens.astimezone(UTC), closes.astimezone(UTC))


def opg_block_reason(
    cal: TradingCalendar, scheduled_open: datetime, now: datetime, safety_seconds: float
) -> str:
    """'' if an opg order for `scheduled_open` may be sent at `now`, else the blocking reason."""
    try:
        w = opg_window(cal, scheduled_open, safety_seconds)
    except ValueError as exc:
        return f"opg_schedule_invalid: {exc}"
    t = _utc(now)
    if t < w.opens_at:
        return "opg_window_not_open"
    if t >= w.closes_at:
        return "opg_cutoff_passed"
    local = t.astimezone(NEW_YORK)
    margin = timedelta(seconds=safety_seconds)
    band_start = datetime.combine(local.date(), OPG_CUTOFF, NEW_YORK) - margin
    band_end = datetime.combine(local.date(), OPG_QUEUE_OPENS, NEW_YORK) + margin
    if band_start <= local < band_end:
        return "opg_rejection_window"
    return ""
