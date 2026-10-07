"""Result records of a backtest. Every record is HYPOTHETICAL: no order was ever sent."""

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class Costs:
    commission: float
    spread: float
    slippage: float

    @property
    def total(self) -> float:
        return self.commission + self.spread + self.slippage


@dataclass(frozen=True)
class Fill:
    """A hypothetical execution: always at a regular-session open (validated by the engine)."""

    bar_index: int
    ts: pd.Timestamp  # session open of the execution bar
    side: str  # "buy" | "sell"
    price: float  # reference price (raw open); costs are separate amounts
    shares: float
    notional: float
    costs: Costs
    signal_observed_at: pd.Timestamp
    reason: str


TRADE_COLUMNS = (
    "trade_id",
    "strategy_id",
    "entry_signal_at",
    "entry_time",
    "entry_price",
    "exit_signal_at",
    "exit_time",
    "exit_price",
    "shares",
    "position",
    "gross_pnl",
    "commission",
    "spread_cost",
    "slippage_cost",
    "total_cost",
    "net_pnl",
    "gross_return",
    "net_return",
    "holding_sessions",
    "holding_days",
    "entry_reason",
    "exit_reason",
)


@dataclass(frozen=True)
class Trade:
    trade_id: int
    strategy_id: str
    entry: Fill
    exit: Fill

    def as_record(self) -> dict[str, Any]:
        gross_pnl = self.exit.notional - self.entry.notional
        costs = self.entry.costs.total + self.exit.costs.total
        committed = self.entry.notional + self.entry.costs.total
        return {
            "trade_id": self.trade_id,
            "strategy_id": self.strategy_id,
            "entry_signal_at": self.entry.signal_observed_at,
            "entry_time": self.entry.ts,
            "entry_price": self.entry.price,
            "exit_signal_at": self.exit.signal_observed_at,
            "exit_time": self.exit.ts,
            "exit_price": self.exit.price,
            "shares": self.entry.shares,
            "position": 1.0,
            "gross_pnl": gross_pnl,
            "commission": self.entry.costs.commission + self.exit.costs.commission,
            "spread_cost": self.entry.costs.spread + self.exit.costs.spread,
            "slippage_cost": self.entry.costs.slippage + self.exit.costs.slippage,
            "total_cost": costs,
            "net_pnl": gross_pnl - costs,
            "gross_return": self.exit.price / self.entry.price - 1,
            "net_return": (gross_pnl - costs) / committed,
            "holding_sessions": self.exit.bar_index - self.entry.bar_index,
            "holding_days": (self.exit.ts - self.entry.ts).days,
            "entry_reason": self.entry.reason,
            "exit_reason": self.exit.reason,
        }


@dataclass(frozen=True)
class OpenPosition:
    """A position still open at the last bar: marked to market, never given a fictitious exit."""

    strategy_id: str
    entry: Fill
    mark_time: pd.Timestamp  # session close of the last bar
    mark_price: float  # last available close

    def as_record(self) -> dict[str, Any]:
        unrealized_gross = self.entry.shares * self.mark_price - self.entry.notional
        return {
            "strategy_id": self.strategy_id,
            "entry_time": self.entry.ts,
            "entry_price": self.entry.price,
            "shares": self.entry.shares,
            "mark_time": self.mark_time,
            "mark_price": self.mark_price,
            "unrealized_gross_pnl": unrealized_gross,
            "entry_costs": self.entry.costs.total,
            "unrealized_net_pnl": unrealized_gross
            - self.entry.costs.total,  # exit costs NOT applied
            "status": "open_marked_to_market",
        }


@dataclass
class BacktestResult:
    strategy_id: str
    equity: pd.DataFrame  # one row per session in the window
    fills: pd.DataFrame
    trades: pd.DataFrame  # completed trades only
    open_position: dict[str, Any] | None
    pending_state: dict[str, Any] | None  # last decision that could not be executed (no next bar)
    metrics: dict[str, float]
    config_fingerprint: str
    meta: dict[str, Any] = field(default_factory=dict)
