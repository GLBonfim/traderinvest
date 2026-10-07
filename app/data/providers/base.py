"""Provider contract. Strategies and ingestion depend on this interface, never on a vendor API."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import ClassVar

import pandas as pd

# Columns every provider must return, in this order.
#   open/high/low/close : prices as delivered by the provider (see provider docs for adjustment)
#   adj_close           : split- and dividend-adjusted close (NaN if the provider has none)
#   volume              : shares
#   split_ratio         : split factor effective on that bar, 0.0 when there is none
BAR_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
    "split_ratio",
)


class ProviderError(Exception):
    """Base class for provider failures."""


class ProviderUnavailableError(ProviderError):
    """Network/API failure; no data could be obtained."""


class ProviderDataError(ProviderError):
    """Provider returned data that violates the contract (shape, columns, timezone)."""


class UnsupportedTimeframeError(ProviderError):
    """Requested timeframe is not implemented for this provider."""


@dataclass(frozen=True)
class ProviderBars:
    """Raw bars as delivered. Index: tz-aware timestamps exactly as the provider labels them.

    No session normalization, validation or correction has been applied.
    """

    provider: str
    provider_symbol: str
    timeframe: str
    frame: pd.DataFrame
    fetched_at: datetime
    provider_version: str


class DataProvider(ABC):
    name: ClassVar[str]
    supported_timeframes: ClassVar[frozenset[str]]

    @abstractmethod
    def fetch_bars(self, symbol: str, timeframe: str, start: date, end: date) -> ProviderBars:
        """Fetch bars whose session date lies in [start, end] (both inclusive)."""
