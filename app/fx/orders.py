"""Order, fill and position records exchanged between the orchestrator and execution adapters.

Units are always base-currency units of the pair (e.g. 10 000 EURUSD = 10 000 EUR). A position
is netted per pair: its signed `units` are > 0 long, < 0 short (no hedging).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.fx.lifecycle import OrderState
from app.fx.models import Side, utc


class ExecutionError(RuntimeError):
    """An adapter failure. `ambiguous` means the order may or may not have reached the venue:
    the caller must look it up by client order id and must never blindly resend."""

    def __init__(self, message: str, *, ambiguous: bool) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str  # deterministic, idempotent at the venue
    intent_id: str
    pair: str
    side: Side
    units: float  # > 0
    created_at: datetime
    reduce_only: bool = False  # an exit: may only reduce or close the position
    stop_loss: float | None = None
    take_profit: float | None = None

    def __post_init__(self) -> None:
        if not self.units > 0:
            raise ValueError("order units must be > 0")
        object.__setattr__(self, "created_at", utc(self.created_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "intent_id": self.intent_id,
            "pair": self.pair,
            "side": self.side.value,
            "units": self.units,
            "created_at": self.created_at.isoformat(),
            "reduce_only": self.reduce_only,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
        }


@dataclass(frozen=True)
class Fill:
    fill_id: str
    client_order_id: str
    pair: str
    side: Side
    units: float
    price: float
    ts: datetime
    commission: float  # account currency, >= 0
    realized_pnl: float  # account currency, gross of this fill's commission
    reason: str = "order"  # order | stop_loss | take_profit | stop_out

    def to_dict(self) -> dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "client_order_id": self.client_order_id,
            "pair": self.pair,
            "side": self.side.value,
            "units": self.units,
            "price": self.price,
            "ts": self.ts.isoformat(),
            "commission": self.commission,
            "realized_pnl": self.realized_pnl,
            "reason": self.reason,
        }


@dataclass
class OrderStatus:
    client_order_id: str
    broker_order_id: str
    pair: str
    side: Side
    units: float
    state: OrderState
    filled_units: float = 0.0
    avg_price: float | None = None
    reason: str = ""
    fills: list[Fill] = field(default_factory=list)

    @property
    def remaining_units(self) -> float:
        """Units not executed. With a final state (CANCELLED/EXPIRED/REJECTED) the remainder
        will never execute; a CANCELLED order with `filled_units > 0` is a partial fill whose
        remainder was cancelled."""
        return max(0.0, self.units - self.filled_units)

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "pair": self.pair,
            "side": self.side.value,
            "units": self.units,
            "state": self.state.value,
            "filled_units": self.filled_units,
            "avg_price": self.avg_price,
            "reason": self.reason,
            "fills": [f.to_dict() for f in self.fills],
        }


@dataclass
class Position:
    pair: str
    units: float  # signed: > 0 long, < 0 short
    avg_price: float
    opened_at: datetime
    position_id: str
    stop_loss: float | None = None
    take_profit: float | None = None

    @property
    def side(self) -> Side:
        return Side.BUY if self.units > 0 else Side.SELL

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "units": self.units,
            "avg_price": self.avg_price,
            "opened_at": self.opened_at.isoformat(),
            "position_id": self.position_id,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Position":
        return cls(
            pair=str(d["pair"]),
            units=float(d["units"]),
            avg_price=float(d["avg_price"]),
            opened_at=datetime.fromisoformat(str(d["opened_at"])),
            position_id=str(d["position_id"]),
            stop_loss=None if d.get("stop_loss") is None else float(d["stop_loss"]),
            take_profit=None if d.get("take_profit") is None else float(d["take_profit"]),
        )


@dataclass(frozen=True)
class AccountSnapshot:
    currency: str
    balance: float  # cash: initial capital + realized P&L - costs
    unrealized: float
    equity: float  # balance + unrealized
    margin_used: float
    free_margin: float
    ts: datetime

    @property
    def margin_level_pct(self) -> float | None:
        return None if self.margin_used <= 0 else 100.0 * self.equity / self.margin_used

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "balance": self.balance,
            "unrealized": self.unrealized,
            "equity": self.equity,
            "margin_used": self.margin_used,
            "free_margin": self.free_margin,
            "margin_level_pct": self.margin_level_pct,
            "ts": self.ts.isoformat(),
        }
