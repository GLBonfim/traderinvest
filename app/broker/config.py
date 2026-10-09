"""Broker sandbox configuration (ADR-0025). SANDBOX / PAPER ONLY — no real money.

The only endpoint is https://paper-api.alpaca.markets. It is a constant, not a setting: there is
no configuration value, parameter or environment variable that selects an endpoint, and
https://api.alpaca.markets is explicitly rejected. Only sandbox (paper) credentials are read, from
environment variables, and never logged, stored or displayed.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BROKER_ROOT = REPO_ROOT / "data" / "broker"  # git-ignored

SANDBOX_VENDOR = "alpaca"
SANDBOX_BASE_URL = "https://paper-api.alpaca.markets"
SANDBOX_HOST = "paper-api.alpaca.markets"
# Never usable; named only so it can be rejected explicitly.
REJECTED_PRODUCTION_HOST = "api.alpaca.markets"
KEY_ID_ENV = "ALPACA_PAPER_API_KEY_ID"
SECRET_ENV = "ALPACA_PAPER_API_SECRET_KEY"  # noqa: S105 - the variable NAME, not a secret

# Order policies (Alpaca documents fractional/notional orders as `day` only, and `opg` as the
# opening-auction order):
OPG_WHOLE_SHARES = "opg_whole_shares"  # market, time_in_force=opg, whole shares (default)
DAY_FRACTIONAL = "day_fractional"  # market, time_in_force=day, fractional qty (not default)
ORDER_POLICIES = (OPG_WHOLE_SHARES, DAY_FRACTIONAL)


@dataclass(frozen=True)
class BrokerConfig:
    """Mirror of risk-approved paper instructions into the external SANDBOX account.

    `allocated_capital` is the fixed notional the sandbox mirror sizes against (target exposure x
    allocated capital / decision close), independent of anything else held in the sandbox
    account; every order is also capped by `max_order_notional` and `max_orders_per_day`.
    """

    vendor: str = SANDBOX_VENDOR
    order_policy: str = OPG_WHOLE_SHARES
    allowed_symbols: tuple[str, ...] = ("SPY",)
    allocated_capital: float = 10_000.0
    max_order_notional: float = 12_000.0
    max_orders_per_day: int = 4
    request_timeout_seconds: float = 10.0
    linked_accounts: tuple[str, ...] = field(default_factory=tuple)  # paper account ids

    def __post_init__(self) -> None:
        if self.vendor != SANDBOX_VENDOR:
            raise ValueError("only the Alpaca paper sandbox is supported")
        if self.order_policy not in ORDER_POLICIES:
            raise ValueError(f"order_policy must be one of {ORDER_POLICIES}")
        if self.allocated_capital <= 0 or self.max_order_notional <= 0:
            raise ValueError("capital limits must be > 0")
        if self.max_orders_per_day < 1 or self.request_timeout_seconds <= 0:
            raise ValueError("invalid order count or timeout")
        if not self.allowed_symbols:
            raise ValueError("at least one allowed symbol is required")

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]
