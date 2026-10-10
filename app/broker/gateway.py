"""BrokerGateway: the project-side abstraction. Vendor objects never leave the adapter.

Only three operations exist on purpose: read the account, read one position, submit one order
built by the executor from a RiskManager-approved paper instruction, and look an order up by its
deterministic client id. There is no cancel/replace/arbitrary-order surface.
"""

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from app.broker.config import REJECTED_PRODUCTION_HOST, SANDBOX_HOST


class BrokerSafetyError(RuntimeError):
    """A safety rule refused the operation (production endpoint, credentials, ...)."""


class ProductionEndpointError(BrokerSafetyError):
    """The URL is not exactly the sandbox endpoint."""


class BrokerRequestError(RuntimeError):
    """The broker answered with an error or could not be reached."""

    def __init__(self, message: str, status: int | None = None, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.ambiguous = ambiguous  # True: the request may or may not have reached the broker


def assert_sandbox_url(url: str) -> str:
    """Accept only https://paper-api.alpaca.markets[/...] — exact host, https, default port,
    no credentials in the URL. https://api.alpaca.markets is rejected explicitly; any other host,
    look-alike, http URL or port is refused as well."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host == REJECTED_PRODUCTION_HOST:
        raise ProductionEndpointError(
            "https://api.alpaca.markets is a production endpoint and is always rejected"
        )
    if parts.scheme != "https":
        raise ProductionEndpointError("broker endpoint must use https")
    if parts.username or parts.password:
        raise ProductionEndpointError("credentials must not be embedded in the URL")
    if host != SANDBOX_HOST or parts.port not in (None, 443):
        raise ProductionEndpointError("only the Alpaca PAPER sandbox endpoint is allowed")
    return url


@dataclass(frozen=True)
class BrokerAccount:
    account_id: str
    status: str
    currency: str
    cash: float
    equity: float
    buying_power: float
    trading_blocked: bool
    account_blocked: bool


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    qty: float
    avg_entry_price: float | None
    market_value: float | None


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: str  # buy | sell
    qty: float
    time_in_force: str  # opg | day
    order_type: str = "market"  # market | limit
    limit_price: float | None = None  # required for limit, forbidden for market (ADR-0027)


@dataclass(frozen=True)
class BrokerOrder:
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: str
    qty: float
    status: str
    filled_qty: float
    filled_avg_price: float | None
    submitted_at: str | None
    filled_at: str | None
    order_type: str | None = None
    limit_price: float | None = None


class BrokerGateway(Protocol):
    name: str
    environment: str  # always "sandbox"

    def account(self) -> BrokerAccount: ...
    def position(self, symbol: str) -> BrokerPosition | None: ...
    def order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...
    def submit(self, request: OrderRequest) -> BrokerOrder: ...
