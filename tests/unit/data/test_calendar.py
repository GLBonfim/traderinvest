from datetime import UTC, date, datetime

import pytest

from app.data.calendar import TradingCalendar


@pytest.fixture(scope="module")
def xnys() -> TradingCalendar:
    return TradingCalendar("XNYS")


def test_holidays_and_weekends_are_not_sessions(xnys: TradingCalendar) -> None:
    s = xnys.sessions(date(2024, 7, 1), date(2024, 7, 9))
    assert [d.date().isoformat() for d in s.index] == [
        "2024-07-01",
        "2024-07-02",
        "2024-07-03",
        "2024-07-05",
        "2024-07-08",
        "2024-07-09",
    ]


def test_early_close_is_detected(xnys: TradingCalendar) -> None:
    s = xnys.sessions(date(2024, 7, 2), date(2024, 7, 5))
    assert s.loc["2024-07-03", "is_early_close"]
    assert s.loc["2024-07-03", "close_utc"] == datetime(2024, 7, 3, 17, 0, tzinfo=UTC)
    assert not s.loc["2024-07-02", "is_early_close"]


def test_session_open_follows_us_daylight_saving(xnys: TradingCalendar) -> None:
    s = xnys.sessions(date(2024, 3, 8), date(2024, 3, 11))
    assert s.loc["2024-03-08", "open_utc"] == datetime(2024, 3, 8, 14, 30, tzinfo=UTC)  # EST
    assert s.loc["2024-03-11", "open_utc"] == datetime(2024, 3, 11, 13, 30, tzinfo=UTC)  # EDT


def test_unscheduled_closures_are_respected(xnys: TradingCalendar) -> None:
    s = xnys.sessions(date(2001, 9, 10), date(2001, 9, 18))
    assert [d.date() for d in s.index] == [date(2001, 9, 10), date(2001, 9, 17), date(2001, 9, 18)]


def test_range_outside_calendar_bounds_is_clipped(xnys: TradingCalendar) -> None:
    assert xnys.sessions(date(1900, 1, 1), date(1900, 12, 31)).empty
    assert xnys.sessions(date(1989, 12, 1), date(1990, 1, 5)).index[0].date() == xnys.first_session
