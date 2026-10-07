"""Exchange trading calendar (offline, deterministic) backed by `exchange_calendars`."""

from datetime import date, time

import exchange_calendars as xcals
import pandas as pd

DEFAULT_CALENDAR_START = date(1990, 1, 1)


class TradingCalendar:
    def __init__(self, exchange: str = "XNYS", start: date = DEFAULT_CALENDAR_START) -> None:
        self.exchange = exchange
        self._cal = xcals.get_calendar(exchange, start=pd.Timestamp(start))
        self.tz: str = str(self._cal.tz)
        self.source = f"exchange_calendars {xcals.__version__}"

    @property
    def first_session(self) -> date:
        return pd.Timestamp(self._cal.first_session).date()

    @property
    def last_session(self) -> date:
        return pd.Timestamp(self._cal.last_session).date()

    def sessions(self, start: date, end: date) -> pd.DataFrame:
        """Sessions in [start, end] (inclusive), clipped to the calendar's bounds.

        Index: session date (naive DatetimeIndex). Columns: open_utc, close_utc (tz=UTC),
        is_early_close (local close earlier than the regular close).
        """
        lo = max(start, self.first_session)
        hi = min(end, self.last_session)
        if lo > hi:
            return pd.DataFrame(
                {
                    "open_utc": pd.Series(dtype="datetime64[ns, UTC]"),
                    "close_utc": pd.Series(dtype="datetime64[ns, UTC]"),
                    "is_early_close": pd.Series(dtype=bool),
                },
                index=pd.DatetimeIndex([], name="session_date"),
            )
        sched = self._cal.schedule.loc[pd.Timestamp(lo) : pd.Timestamp(hi), ["open", "close"]]
        regular_close = self._regular_close()
        local_close = sched["close"].dt.tz_convert(self.tz)
        out = pd.DataFrame(
            {
                "open_utc": sched["open"],
                "close_utc": sched["close"],
                "is_early_close": [t.time() < regular_close for t in local_close],
            },
            index=pd.DatetimeIndex(sched.index, name="session_date"),
        )
        return out

    def _regular_close(self) -> time:
        closes = self._cal.close_times
        return closes[-1][1]  # type: ignore[no-any-return]
