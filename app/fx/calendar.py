"""Forex trading calendar (ADR-0038): 24/5 market, trading day ending at 17:00 New York.

Conventions:
- The FX trading day D runs from 17:00 America/New_York on the previous calendar day to 17:00
  New York on D (the rollover). Daily risk limits, entry counts and daily P&L all use it.
- The market is open from Sunday 17:00 New York to Friday 17:00 New York. DST is handled by the
  IANA zone (`zoneinfo`), so the boundary stays at 17:00 local New York time all year.
- Holidays are configurable whole trading days (e.g. 25 December, 1 January) on which the
  simulator produces no prices and the orchestrator opens no positions. Real liquidity on
  holidays differs by venue; a broker's own calendar wins when one is connected.
- Sessions (Sydney, Tokyo, London, New York) are informational: they are defined in their own
  local zones, so their UTC times move with each zone's DST.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.fx.models import utc

NEW_YORK = ZoneInfo("America/New_York")
ROLLOVER = time(17, 0)
_SHIFT = timedelta(hours=7)  # 17:00 New York + 7h = 00:00 of the next trading day


@dataclass(frozen=True)
class Session:
    name: str
    zone: str
    opens: time
    closes: time

    def is_active(self, ts: datetime) -> bool:
        local = ts.astimezone(ZoneInfo(self.zone))
        if local.weekday() >= 5:  # a local weekend: the session does not trade
            return False
        return self.opens <= local.time() < self.closes


SESSIONS: tuple[Session, ...] = (
    Session("Sydney", "Australia/Sydney", time(7, 0), time(16, 0)),
    Session("Tokyo", "Asia/Tokyo", time(9, 0), time(18, 0)),
    Session("London", "Europe/London", time(8, 0), time(17, 0)),
    Session("New York", "America/New_York", time(8, 0), time(17, 0)),
)


def default_holidays(years: Iterable[int]) -> frozenset[date]:
    """25 December and 1 January of each year (when they fall on a trading weekday)."""
    out: set[date] = set()
    for y in years:
        out.update(d for d in (date(y, 12, 25), date(y, 1, 1)) if d.weekday() < 5)
    return frozenset(out)


@dataclass(frozen=True)
class FxCalendar:
    holidays: frozenset[date] = field(default_factory=lambda: default_holidays(range(2020, 2041)))

    def trading_day(self, ts: datetime) -> date:
        """The FX trading day that contains `ts` (17:00 New York rollover)."""
        return (utc(ts).astimezone(NEW_YORK) + _SHIFT).date()

    def day_bounds(self, day: date) -> tuple[datetime, datetime]:
        """[start, end) of trading day `day`, in UTC."""
        start = datetime.combine(day - timedelta(days=1), ROLLOVER, NEW_YORK)
        end = datetime.combine(day, ROLLOVER, NEW_YORK)
        return start.astimezone(UTC), end.astimezone(UTC)

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def is_open(self, ts: datetime) -> bool:
        return self.is_trading_day(self.trading_day(ts))

    def next_open(self, ts: datetime) -> datetime:
        """`ts` itself when the market is open, else the start of the next trading day."""
        if self.is_open(ts):
            return utc(ts)
        day = self.trading_day(ts) + timedelta(days=1)
        while not self.is_trading_day(day):
            day += timedelta(days=1)
        return self.day_bounds(day)[0]

    def next_close(self, ts: datetime) -> datetime:
        """The end of the current trading day (the next rollover after `ts`)."""
        return self.day_bounds(self.trading_day(ts))[1]

    def active_sessions(self, ts: datetime) -> list[str]:
        if not self.is_open(ts):
            return []
        return [s.name for s in SESSIONS if s.is_active(utc(ts))]
