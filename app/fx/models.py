"""Forex market models: currency pairs, quotes, bars and the origin of every price.

Every price carries its `DataOrigin`, so nothing synthetic can be shown or used as if it were a
real market price. Prices are floats (quote currency per unit of base currency); amounts of
money carry their currency explicitly.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class DataOrigin(StrEnum):
    """Where a price comes from. Only LIVE is a real-time market price."""

    SYNTHETIC = "synthetic"  # generated locally (simulation, tests); never a market price
    HISTORICAL = "historical"  # recorded market data, replayed
    DELAYED = "delayed"  # real market data with a known delay
    LIVE = "live"  # real-time market data from an identified provider


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"

    @property
    def entry_side(self) -> Side:
        if self is Direction.FLAT:
            raise ValueError("a FLAT direction has no entry side")
        return Side.BUY if self is Direction.LONG else Side.SELL


@dataclass(frozen=True)
class Pair:
    """A currency pair BASE/QUOTE: the price is QUOTE per one unit of BASE."""

    base: str
    quote: str
    pip_size: float

    def __post_init__(self) -> None:
        if len(self.base) != 3 or len(self.quote) != 3 or self.base == self.quote:
            raise ValueError(f"invalid currency pair {self.base}/{self.quote}")
        if not self.base.isupper() or not self.quote.isupper():
            raise ValueError("currency codes must be upper-case ISO 4217 codes")
        if not self.pip_size > 0:
            raise ValueError("pip_size must be > 0")

    @property
    def symbol(self) -> str:
        return f"{self.base}{self.quote}"

    @property
    def display(self) -> str:
        return f"{self.base}/{self.quote}"

    def pips(self, price_difference: float) -> float:
        return price_difference / self.pip_size


# Provisional catalogue (configurable): major pairs. JPY pairs quote to 2 decimals (pip 0.01).
PAIRS: dict[str, Pair] = {
    p.symbol: p
    for p in (
        Pair("EUR", "USD", 0.0001),
        Pair("GBP", "USD", 0.0001),
        Pair("USD", "JPY", 0.01),
        Pair("AUD", "USD", 0.0001),
        Pair("USD", "CHF", 0.0001),
        Pair("USD", "CAD", 0.0001),
        Pair("NZD", "USD", 0.0001),
        Pair("EUR", "GBP", 0.0001),
        Pair("EUR", "JPY", 0.01),
    )
}


def pair(symbol: str) -> Pair:
    key = symbol.replace("/", "").upper()
    if key not in PAIRS:
        raise KeyError(f"unknown currency pair {symbol!r}")
    return PAIRS[key]


def utc(ts: datetime) -> datetime:
    """A timezone-aware UTC datetime; naive datetimes are refused (never guessed)."""
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return ts.astimezone(UTC)


@dataclass(frozen=True)
class Quote:
    pair: str  # symbol, e.g. "EURUSD"
    bid: float
    ask: float
    ts: datetime  # UTC
    origin: DataOrigin
    source: str  # provider or generator identity

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", utc(self.ts))

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def price_for(self, side: Side) -> float:
        """Execution price of a market order: BUY at the ask, SELL at the bid."""
        return self.ask if side is Side.BUY else self.bid

    def mark_for(self, units: float) -> float:
        """Liquidation price of a position: a long closes at the bid, a short at the ask."""
        return self.bid if units > 0 else self.ask


@dataclass(frozen=True)
class Bar:
    """A mid-price OHLC bar with its average spread (quote currency)."""

    pair: str
    ts: datetime  # bar open, UTC
    open: float
    high: float
    low: float
    close: float
    spread: float
    origin: DataOrigin

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", utc(self.ts))
