"""Synthetic, deterministic fixtures. These numbers are NOT market data."""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import ClassVar

import pandas as pd

from app.data.providers.base import DataProvider, ProviderBars
from app.data.providers.yfinance_provider import YFinanceProvider

# (open, high, low, close, adj_close, volume)
Row = tuple[float, float, float, float, float, float]

# NYSE sessions 2024-07-01..2024-07-09: 07-04 is a holiday, 07-03 closes early (13:00 ET).
SESSIONS_JULY_2024 = [
    "2024-07-01",
    "2024-07-02",
    "2024-07-03",
    "2024-07-05",
    "2024-07-08",
    "2024-07-09",
]


def row(close: float, volume: float = 1_000_000, factor: float = 0.98) -> Row:
    return (close, close + 1, close - 1, close, close * factor, volume)


def yahoo_frame(rows: Mapping[str, Row], splits: Mapping[str, float] | None = None) -> pd.DataFrame:
    """Shape of yfinance `history(auto_adjust=False, actions=True)`: local-midnight NY index."""
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in rows], name="Date").tz_localize(
        "America/New_York"
    )
    values = list(rows.values())
    splits = splits or {}
    return pd.DataFrame(
        {
            "Open": [v[0] for v in values],
            "High": [v[1] for v in values],
            "Low": [v[2] for v in values],
            "Close": [v[3] for v in values],
            "Adj Close": [v[4] for v in values],
            "Volume": [v[5] for v in values],
            "Dividends": 0.0,
            "Stock Splits": [splits.get(d, 0.0) for d in rows],
            "Capital Gains": 0.0,
        },
        index=idx,
    )


def provider_bars(
    rows: Mapping[str, Row], splits: Mapping[str, float] | None = None
) -> ProviderBars:
    frame = yahoo_frame(rows, splits)
    provider = YFinanceProvider(fetcher=lambda *_: frame)
    dates = [date.fromisoformat(d) for d in rows]
    return provider.fetch_bars("SPY", "1d", min(dates), max(dates))


def regular_rows(start_close: float = 100.0) -> dict[str, Row]:
    return {d: row(start_close + i) for i, d in enumerate(SESSIONS_JULY_2024)}


AFTER_JULY_2024 = datetime(2024, 8, 1, tzinfo=UTC)


class StubProvider(DataProvider):
    """In-memory provider; `rows` can be mutated between runs to simulate revisions."""

    name: ClassVar[str] = "yfinance"  # reuse SPY's registered mapping
    supported_timeframes: ClassVar[frozenset[str]] = frozenset({"1d"})

    def __init__(self, rows: dict[str, Row], fail: Exception | None = None) -> None:
        self.rows = rows
        self.fail = fail

    def fetch_bars(self, symbol: str, timeframe: str, start: date, end: date) -> ProviderBars:
        frame = yahoo_frame(self.rows)
        fetcher_error = self.fail

        def fetch(*_: object) -> pd.DataFrame:
            if fetcher_error is not None:
                raise fetcher_error
            return frame

        return YFinanceProvider(fetcher=fetch).fetch_bars(symbol, timeframe, start, end)
