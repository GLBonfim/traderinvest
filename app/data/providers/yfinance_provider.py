"""Yahoo Finance via yfinance — PROTOTYPING ONLY (unofficial API, ToS-restricted).

Price semantics (Yahoo, `auto_adjust=False`):
- open/high/low/close are SPLIT-adjusted but NOT dividend-adjusted. They equal as-traded
  prices only for periods without later splits (SPY has had no splits).
- adj_close is split- AND dividend-adjusted (total-return series). It is recomputed by Yahoo
  after every dividend, so historical adj_close values legitimately change over time.
- `repair=False`: we never let the library "fix" prices; problems are reported by validation.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import ClassVar

import pandas as pd
import yfinance as yf

from app.core.logging import get_logger
from app.data.providers.base import (
    BAR_COLUMNS,
    DataProvider,
    ProviderBars,
    ProviderDataError,
    ProviderUnavailableError,
    UnsupportedTimeframeError,
)

log = get_logger(__name__)

# (symbol, start, end_exclusive) -> yfinance-shaped DataFrame. Injectable for offline tests.
HistoryFetcher = Callable[[str, date, date], pd.DataFrame]

_COLUMN_MAP = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Adj Close": "adj_close",
    "Volume": "volume",
}


def _yahoo_history(symbol: str, start: date, end_exclusive: date) -> pd.DataFrame:
    frame: pd.DataFrame = yf.Ticker(symbol).history(
        start=start.isoformat(),
        end=end_exclusive.isoformat(),
        interval="1d",
        auto_adjust=False,
        back_adjust=False,
        actions=True,
        repair=False,
        raise_errors=True,
    )
    return frame


class YFinanceProvider(DataProvider):
    name: ClassVar[str] = "yfinance"
    supported_timeframes: ClassVar[frozenset[str]] = frozenset({"1d"})

    def __init__(self, fetcher: HistoryFetcher | None = None) -> None:
        self._fetch = fetcher or _yahoo_history

    def fetch_bars(self, symbol: str, timeframe: str, start: date, end: date) -> ProviderBars:
        if timeframe not in self.supported_timeframes:
            raise UnsupportedTimeframeError(f"{self.name} supports {sorted(self.supported_timeframes)}")
        if start > end:
            raise ValueError("start must be <= end")

        fetched_at = datetime.now(UTC)
        try:
            raw = self._fetch(symbol, start, end + timedelta(days=1))
        except Exception as exc:  # any library/network failure
            log.error("provider.fetch_failed", provider=self.name, symbol=symbol, error=repr(exc))
            raise ProviderUnavailableError(f"{type(exc).__name__}: {exc}") from exc

        frame = self._to_contract(raw)
        log.info(
            "provider.fetched",
            provider=self.name,
            symbol=symbol,
            timeframe=timeframe,
            rows=len(frame),
            start=start.isoformat(),
            end=end.isoformat(),
        )
        return ProviderBars(
            provider=self.name,
            provider_symbol=symbol,
            timeframe=timeframe,
            frame=frame,
            fetched_at=fetched_at,
            provider_version=f"yfinance {yf.__version__}",
        )

    @staticmethod
    def _to_contract(raw: pd.DataFrame) -> pd.DataFrame:
        if raw.empty:
            return pd.DataFrame(
                {c: pd.Series(dtype="float64") for c in BAR_COLUMNS},
                index=pd.DatetimeIndex([], tz=UTC, name="provider_ts"),
            )
        missing = set(_COLUMN_MAP) - set(raw.columns)
        if missing:
            raise ProviderDataError(f"missing columns: {sorted(missing)}")
        if not isinstance(raw.index, pd.DatetimeIndex) or raw.index.tz is None:
            raise ProviderDataError("index must be a timezone-aware DatetimeIndex")

        frame = raw[list(_COLUMN_MAP)].rename(columns=_COLUMN_MAP).astype("float64")
        splits = raw["Stock Splits"] if "Stock Splits" in raw.columns else 0.0
        frame["split_ratio"] = pd.Series(splits, index=raw.index, dtype="float64").fillna(0.0)
        frame.index = raw.index.rename("provider_ts")
        return frame[list(BAR_COLUMNS)]
