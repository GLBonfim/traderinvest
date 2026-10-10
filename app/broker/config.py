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

# Order policies (Alpaca documents fractional/notional orders as `day` market only, and `opg`
# with market/limit as market-on-open / limit-on-open opening-auction orders):
# buys: LIMIT-on-open at a budget-bounded limit price; sells: market-on-open (default, ADR-0027)
OPG_LOO_BUYS = "opg_whole_shares_loo_buys"
OPG_WHOLE_SHARES = "opg_whole_shares"  # market-on-open both sides, whole shares
DAY_FRACTIONAL = "day_fractional"  # market, time_in_force=day, fractional qty (not default)
ORDER_POLICIES = (OPG_LOO_BUYS, OPG_WHOLE_SHARES, DAY_FRACTIONAL)
WHOLE_SHARE_POLICIES = (OPG_LOO_BUYS, OPG_WHOLE_SHARES)  # all time_in_force=opg

# Capital policy for BUY orders (ADR-0026). A market order (MOO or day) has no maximum price, so
# its cost cannot be bounded by `allocated_capital` before execution.
CAPITAL_BLOCK_UNBOUNDED = "block_unbounded"  # default: a buy with no price bound is never sent
# Checks the budget at the decision close only. NOT a guarantee: the fill price can be higher.
# Exists for tests of the submission mechanics and as an explicit owner choice; never default.
CAPITAL_REFERENCE_PRICE_UNGUARANTEED = "reference_price_unguaranteed"
CAPITAL_POLICIES = (CAPITAL_BLOCK_UNBOUNDED, CAPITAL_REFERENCE_PRICE_UNGUARANTEED)


@dataclass(frozen=True)
class BrokerConfig:
    """Mirror of risk-approved paper instructions into the external SANDBOX account.

    `allocated_capital` is the fixed notional the sandbox mirror sizes against (target exposure x
    allocated capital / decision close), independent of anything else held in the sandbox
    account; every order is also capped by `max_order_notional` and `max_orders_per_day`.

    Three separate limits (ADR-0026): `allocated_capital` is the sandbox BUDGET (cost of the
    resulting position, enforced per `capital_policy`); `max_order_notional` / daily count are
    RISK limits on a single order; the Alpaca account's cash / buying power is checked
    independently before a buy (no margin is ever used).

    Under `opg_whole_shares_loo_buys` (ADR-0027) buys are sized on the EFFECTIVE budget
    `allocated_capital - order_cost_allowance` and sent as limit-on-open orders whose limit price
    keeps the resulting position's value at the fill price within that effective budget.
    """

    vendor: str = SANDBOX_VENDOR
    order_policy: str = OPG_LOO_BUYS
    allowed_symbols: tuple[str, ...] = ("SPY",)
    allocated_capital: float = 10_000.0
    max_order_notional: float = 12_000.0
    max_orders_per_day: int = 4
    request_timeout_seconds: float = 10.0
    capital_policy: str = CAPITAL_BLOCK_UNBOUNDED
    opg_safety_seconds: float = 60.0  # margin on each OPG window boundary
    # Reserved for per-order costs under the LOO policy: Alpaca charges no commission on equity
    # buys; 1.00 mirrors the project's flat research commission (BacktestConfig) as a margin.
    order_cost_allowance: float = 1.0
    linked_accounts: tuple[str, ...] = field(default_factory=tuple)  # paper account ids

    def __post_init__(self) -> None:
        if self.vendor != SANDBOX_VENDOR:
            raise ValueError("only the Alpaca paper sandbox is supported")
        if self.order_policy not in ORDER_POLICIES:
            raise ValueError(f"order_policy must be one of {ORDER_POLICIES}")
        if self.capital_policy not in CAPITAL_POLICIES:
            raise ValueError(f"capital_policy must be one of {CAPITAL_POLICIES}")
        if self.opg_safety_seconds < 0:
            raise ValueError("opg_safety_seconds must be >= 0")
        if self.allocated_capital <= 0 or self.max_order_notional <= 0:
            raise ValueError("capital limits must be > 0")
        if not 0 <= self.order_cost_allowance < self.allocated_capital:
            raise ValueError("order_cost_allowance must be in [0, allocated_capital)")
        if self.max_orders_per_day < 1 or self.request_timeout_seconds <= 0:
            raise ValueError("invalid order count or timeout")
        if not self.allowed_symbols:
            raise ValueError("at least one allowed symbol is required")

    @property
    def sizing_capital(self) -> float:
        """Capital the target quantity is sized on: the effective budget under the LOO policy."""
        if self.order_policy == OPG_LOO_BUYS:
            return self.allocated_capital - self.order_cost_allowance
        return self.allocated_capital

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:16]
