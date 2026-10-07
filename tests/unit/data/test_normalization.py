from datetime import UTC, date, datetime

import pandas as pd
import pytest

from app.data.calendar import TradingCalendar
from app.data.normalization import normalize_daily
from app.data.providers.base import ProviderBars
from tests.factories import provider_bars, regular_rows, row


@pytest.fixture(scope="module")
def xnys() -> TradingCalendar:
    return TradingCalendar("XNYS")


def test_daily_bars_are_stamped_with_session_open_utc(xnys: TradingCalendar) -> None:
    out = normalize_daily(provider_bars(regular_rows()), xnys)
    assert out.issues == []
    assert out.frame.index[0] == datetime(2024, 7, 1, 13, 30, tzinfo=UTC)
    assert str(out.frame.index.tz) == "UTC"
    early = out.frame[out.frame["session_date"] == date(2024, 7, 3)]
    assert early["close_utc"].iloc[0] == datetime(2024, 7, 3, 17, 0, tzinfo=UTC)


def test_prices_are_not_modified(xnys: TradingCalendar) -> None:
    bars = provider_bars(regular_rows())
    out = normalize_daily(bars, xnys)
    assert out.frame["close"].tolist() == bars.frame["close"].tolist()
    assert out.frame["volume"].tolist() == bars.frame["volume"].tolist()


def test_non_session_dates_are_excluded_and_reported(xnys: TradingCalendar) -> None:
    rows = regular_rows()
    rows["2024-07-04"] = row(999)  # Independence Day
    rows["2024-07-06"] = row(999)  # Saturday
    out = normalize_daily(provider_bars(dict(sorted(rows.items()))), xnys)
    assert len(out.frame) == 6
    assert sorted(i.details["session_date"] for i in out.issues) == ["2024-07-04", "2024-07-06"]
    assert {(i.check_name, i.action_taken) for i in out.issues} == {("non_session_date", "excluded")}


def test_non_midnight_labels_are_flagged_but_mapped_by_local_date(xnys: TradingCalendar) -> None:
    bars = provider_bars(regular_rows())
    shifted = bars.frame.copy()
    shifted.index = shifted.index + pd.Timedelta(hours=16)  # 16:00 NY, same local date
    out = normalize_daily(
        ProviderBars(bars.provider, bars.provider_symbol, "1d", shifted, bars.fetched_at, "x"), xnys
    )
    assert len(out.frame) == 6
    assert {i.check_name for i in out.issues} == {"unexpected_daily_label_time"}
    assert out.frame.index[0] == datetime(2024, 7, 1, 13, 30, tzinfo=UTC)
