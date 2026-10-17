"""Ports between the trading core and the outside world (hexagonal architecture, ADR-0036).

The orchestrator, agents and risk manager depend only on these Protocols. Each environment
supplies its own adapters (SIMULATION: `app.fx.sim`; DEMO: a broker's practice API). An
adapter declares its environment and capabilities; it never falls back to another environment.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from app.fx.environment import Environment
from app.fx.models import Bar, DataOrigin, Quote
from app.fx.orders import AccountSnapshot, OrderRequest, OrderStatus, Position


@dataclass(frozen=True)
class Capabilities:
    environment: Environment
    data_origin: DataOrigin
    server_side_stops: bool  # stop loss / take profit held by the venue
    partial_fills: bool
    hedging: bool  # False: positions are netted per pair
    min_units: float
    unit_step: float
    max_leverage: float
    description: str


class MarketDataProvider(Protocol):
    @property
    def origin(self) -> DataOrigin: ...

    def pairs(self) -> Sequence[str]: ...

    def latest(self, pair: str) -> Quote | None: ...

    def bars(self, pair: str, timeframe: str, count: int) -> list[Bar]: ...


class AccountProvider(Protocol):
    def account(self) -> AccountSnapshot: ...


class PositionProvider(Protocol):
    def positions(self) -> list[Position]: ...


class OrderExecutionProvider(Protocol):
    @property
    def capabilities(self) -> Capabilities: ...

    def submit(self, request: OrderRequest) -> OrderStatus:
        """Sends an order. Idempotent by `client_order_id`: a repeated id returns the existing
        order. Raises `ExecutionError(ambiguous=True)` when the outcome is unknown."""
        ...

    def order(self, client_order_id: str) -> OrderStatus | None: ...


class BrokerAdapter(AccountProvider, PositionProvider, OrderExecutionProvider, Protocol):
    """The full set a venue offers to the orchestrator."""


class TradingCalendar(Protocol):
    def trading_day(self, ts: datetime) -> date: ...

    def is_open(self, ts: datetime) -> bool: ...

    def day_bounds(self, day: date) -> tuple[datetime, datetime]: ...
