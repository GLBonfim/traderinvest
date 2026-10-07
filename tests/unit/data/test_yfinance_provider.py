from datetime import date

import pandas as pd
import pytest

from app.data.providers.base import (
    BAR_COLUMNS,
    ProviderDataError,
    ProviderUnavailableError,
    UnsupportedTimeframeError,
)
from app.data.providers.yfinance_provider import YFinanceProvider
from tests.factories import regular_rows, yahoo_frame


def test_maps_yahoo_columns_to_contract_without_altering_values() -> None:
    raw = yahoo_frame(regular_rows(), splits={"2024-07-05": 2.0})
    bars = YFinanceProvider(fetcher=lambda *_: raw).fetch_bars(
        "SPY", "1d", date(2024, 7, 1), date(2024, 7, 9)
    )
    assert tuple(bars.frame.columns) == BAR_COLUMNS
    assert bars.provider == "yfinance"
    assert bars.provider_version.startswith("yfinance ")
    assert (bars.frame.index == raw.index).all()  # labels untouched: normalization is separate
    assert bars.frame["close"].tolist() == raw["Close"].tolist()
    assert bars.frame["adj_close"].tolist() == raw["Adj Close"].tolist()
    assert bars.frame.loc["2024-07-05", "split_ratio"].item() == 2.0


def test_end_date_is_inclusive() -> None:
    calls: list[tuple[str, date, date]] = []

    def fetch(symbol: str, start: date, end: date) -> pd.DataFrame:
        calls.append((symbol, start, end))
        return yahoo_frame(regular_rows())

    YFinanceProvider(fetcher=fetch).fetch_bars("SPY", "1d", date(2024, 7, 1), date(2024, 7, 9))
    assert calls == [("SPY", date(2024, 7, 1), date(2024, 7, 10))]


def test_network_failures_become_provider_unavailable() -> None:
    def boom(*_: object) -> pd.DataFrame:
        raise ConnectionError("dns failure")

    with pytest.raises(ProviderUnavailableError, match="ConnectionError"):
        YFinanceProvider(fetcher=boom).fetch_bars("SPY", "1d", date(2024, 7, 1), date(2024, 7, 2))


def test_missing_columns_are_a_contract_violation() -> None:
    raw = yahoo_frame(regular_rows()).drop(columns=["Adj Close"])
    with pytest.raises(ProviderDataError, match="Adj Close"):
        YFinanceProvider(fetcher=lambda *_: raw).fetch_bars(
            "SPY", "1d", date(2024, 7, 1), date(2024, 7, 9)
        )


def test_naive_timestamps_are_a_contract_violation() -> None:
    raw = yahoo_frame(regular_rows())
    raw.index = raw.index.tz_localize(None)
    with pytest.raises(ProviderDataError, match="timezone"):
        YFinanceProvider(fetcher=lambda *_: raw).fetch_bars(
            "SPY", "1d", date(2024, 7, 1), date(2024, 7, 9)
        )


def test_empty_response_returns_empty_contract_frame() -> None:
    bars = YFinanceProvider(fetcher=lambda *_: pd.DataFrame()).fetch_bars(
        "SPY", "1d", date(2024, 7, 1), date(2024, 7, 9)
    )
    assert bars.frame.empty
    assert tuple(bars.frame.columns) == BAR_COLUMNS


def test_only_daily_timeframe_is_supported() -> None:
    with pytest.raises(UnsupportedTimeframeError):
        YFinanceProvider(fetcher=lambda *_: pd.DataFrame()).fetch_bars(
            "SPY", "1h", date(2024, 7, 1), date(2024, 7, 9)
        )
