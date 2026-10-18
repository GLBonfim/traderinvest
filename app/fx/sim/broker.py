"""Simulated FX broker (SIMULATION only): a netting margin account that fills market orders at
the synthetic bid/ask.

Behaviour, chosen to resemble a retail FX venue:
- market orders fill at the ask (buy) / bid (sell), plus configurable slippage in pips;
- commission per million of notional, in account currency;
- idempotent by `client_order_id`: a repeated id returns the existing order, never a new one;
- server-side stop loss / take profit, checked on every quote; a gap fills at the quote, not at
  the stop level (no guaranteed stops);
- margin close-out: when equity / used margin falls below `stop_out_level_pct` the largest
  losing position is closed (ESMA retail convention: 50 %);
- injectable faults for tests and drills: reject, timeout after acceptance (ambiguous), partial
  fill (reported as CANCELLED with `filled_units > 0`: the remainder is cancelled).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.fx.accounting import EPS, Book, Converter
from app.fx.environment import Environment
from app.fx.lifecycle import OrderState
from app.fx.models import DataOrigin, Quote, Side, pair
from app.fx.orders import (
    AccountSnapshot,
    ExecutionError,
    Fill,
    OrderRequest,
    OrderStatus,
    Position,
)
from app.fx.ports import Capabilities


@dataclass
class SimBrokerConfig:
    currency: str = "EUR"
    initial_balance: float = 10_000.0
    leverage: float = 30.0
    commission_per_million: float = 30.0  # account currency per 1M notional, per side
    slippage_pips: float = 0.1
    min_units: float = 1_000.0
    unit_step: float = 1_000.0
    stop_out_level_pct: float = 50.0


@dataclass
class Faults:
    """One-shot faults applied to the next submitted orders (tests and drills)."""

    reject_next: int = 0
    timeout_next: int = 0  # the order is accepted and filled, but the reply is lost
    partial_next: int = 0
    partial_fraction: float = 0.5
    reject_reason: str = "simulated rejection"


@dataclass
class SimBroker:
    config: SimBrokerConfig = field(default_factory=SimBrokerConfig)
    faults: Faults = field(default_factory=Faults)

    def __post_init__(self) -> None:
        c = self.config
        self.book = Book(c.currency, c.initial_balance, c.leverage)
        self.orders: dict[str, OrderStatus] = {}
        self.quotes: dict[str, Quote] = {}
        self.events: list[Fill] = []  # fills not yet collected by the orchestrator
        self.closed_positions: list[dict[str, Any]] = []
        self.now: datetime | None = None

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            environment=Environment.SIMULATION,
            data_origin=DataOrigin.SYNTHETIC,
            server_side_stops=True,
            partial_fills=True,
            hedging=False,
            min_units=self.config.min_units,
            unit_step=self.config.unit_step,
            max_leverage=self.config.leverage,
            description="Corretor simulado local (sem dinheiro real, sem serviço externo)",
            # the simulator's order book IS the venue, in this process: an id it does not hold
            # was never accepted, so an empty lookup is a confirmed absence
            authoritative_order_lookup=True,
        )

    # --- market ------------------------------------------------------------------------------

    def on_quotes(self, quotes: Mapping[str, Quote]) -> list[Fill]:
        """Updates prices, triggers server-side stops/targets and the margin close-out."""
        self.quotes.update(quotes)
        if quotes:
            self.now = max(q.ts for q in quotes.values())
        fills: list[Fill] = []
        for sym in list(self.book.positions):
            pos = self.book.positions[sym]
            q = self.quotes.get(sym)
            if q is None:
                continue
            mark = q.mark_for(pos.units)
            long = pos.units > 0
            if pos.stop_loss is not None and (
                (long and mark <= pos.stop_loss) or (not long and mark >= pos.stop_loss)
            ):
                fills.append(self._close(sym, "stop_loss"))
            elif pos.take_profit is not None and (
                (long and mark >= pos.take_profit) or (not long and mark <= pos.take_profit)
            ):
                fills.append(self._close(sym, "take_profit"))
        fills.extend(self._margin_close_out())
        return fills

    def _margin_close_out(self) -> list[Fill]:
        fills: list[Fill] = []
        while self.book.positions:
            snap = self.account()
            level = snap.margin_level_pct
            if level is None or level >= self.config.stop_out_level_pct:
                break
            conv = Converter(self.quotes)
            worst = min(
                self.book.positions.values(),
                key=lambda p: (
                    p.units
                    * (self.quotes[p.pair].mark_for(p.units) - p.avg_price)
                    * conv.rate(pair(p.pair).quote, self.book.currency)
                ),
            )
            fills.append(self._close(worst.pair, "stop_out"))
        return fills

    def _close(self, sym: str, reason: str) -> Fill:
        pos = self.book.positions[sym]
        side = pos.side.opposite
        coid = f"{reason}-{pos.position_id}"
        return self._execute(coid, sym, side, abs(pos.units), reason)

    def _price(self, sym: str, side: Side) -> float:
        q = self.quotes.get(sym)
        if q is None:
            raise ExecutionError(f"no price for {sym}", ambiguous=False)
        pip = pair(sym).pip_size
        slip = self.config.slippage_pips * pip
        return round(q.price_for(side) + side.sign * slip, 3 if pip >= 0.01 else 5)

    def _execute(
        self,
        coid: str,
        sym: str,
        side: Side,
        units: float,
        reason: str,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> Fill:
        if self.now is None:
            raise ExecutionError("the simulated broker has no prices yet", ambiguous=False)
        price = self._price(sym, side)
        conv = Converter(self.quotes)
        commission = (
            self.book.notional(sym, units, conv) / 1_000_000 * self.config.commission_per_million
        )
        pos_before = self.book.positions.get(sym)
        before = None if pos_before is None else Position.from_dict(pos_before.to_dict())
        res = self.book.apply_fill(
            sym, side, units, price, self.now, commission, conv, stop_loss, take_profit
        )
        fill = Fill(
            self.book.next_id("fill"),
            coid,
            sym,
            side,
            units,
            price,
            self.now,
            commission,
            res.realized,
            reason,
        )
        if res.closed_position is not None and before is not None:
            self.closed_positions.append(
                {
                    **before.to_dict(),
                    "closed_at": self.now.isoformat(),
                    "exit_price": price,
                    "reason": reason,
                    "realized": res.realized,
                }
            )
        self.events.append(fill)
        return fill

    # --- OrderExecutionProvider ---------------------------------------------------------------

    def submit(self, request: OrderRequest) -> OrderStatus:
        existing = self.orders.get(request.client_order_id)
        if existing is not None:
            return existing  # idempotent: never a second order for the same id
        bid = f"sim-{len(self.orders) + 1:07d}"
        status = OrderStatus(
            request.client_order_id,
            bid,
            request.pair,
            request.side,
            request.units,
            OrderState.ACCEPTED,
        )
        self.orders[request.client_order_id] = status
        f = self.faults
        if f.reject_next > 0:
            f.reject_next -= 1
            status.state, status.reason = OrderState.REJECTED, f.reject_reason
            return status
        units = request.units
        pos = self.book.positions.get(request.pair)
        if request.reduce_only:
            if pos is None or pos.side is request.side:
                status.state, status.reason = OrderState.REJECTED, "nothing to reduce"
                return status
            units = min(units, abs(pos.units))
        try:
            self._check_margin(request, units)
        except ExecutionError as exc:
            status.state, status.reason = OrderState.REJECTED, str(exc)
            return status
        partial = f.partial_next > 0
        if partial:
            f.partial_next -= 1
            step = self.config.unit_step
            units = max(step, (units * f.partial_fraction) // step * step)
        fill = self._execute(
            request.client_order_id,
            request.pair,
            request.side,
            units,
            "order",
            request.stop_loss,
            request.take_profit,
        )
        status.fills.append(fill)
        status.filled_units = units
        status.avg_price = fill.price
        # a market order partly filled has its remainder cancelled at once (immediate-or-
        # cancel): the venue reports a final CANCELLED order with filled_units > 0
        status.state = OrderState.CANCELLED if partial else OrderState.FILLED
        if partial:
            status.reason = "partial fill; remainder cancelled"
        if f.timeout_next > 0:
            f.timeout_next -= 1
            raise ExecutionError("simulated timeout: reply lost after acceptance", ambiguous=True)
        return status

    def _check_margin(self, request: OrderRequest, units: float) -> None:
        if request.reduce_only:
            return
        conv = Converter(self.quotes)
        if request.pair not in self.quotes:
            raise ExecutionError(f"no price for {request.pair}", ambiguous=False)
        pos = self.book.positions.get(request.pair)
        cur = 0.0 if pos is None else pos.units
        new = cur + request.side.sign * units
        added = (abs(new) - abs(cur)) * conv.rate(pair(request.pair).base, self.book.currency)
        if added <= EPS:
            return
        snap = self.account()
        if snap.free_margin < added / self.book.leverage:
            raise ExecutionError("insufficient margin", ambiguous=False)

    def order(self, client_order_id: str) -> OrderStatus | None:
        return self.orders.get(client_order_id)

    # --- Account / positions ------------------------------------------------------------------

    def account(self) -> AccountSnapshot:
        if self.now is None:
            b = self.book.balance
            return AccountSnapshot(
                self.book.currency, b, 0.0, b, 0.0, b, datetime.fromtimestamp(0, UTC)
            )
        return self.book.snapshot(self.quotes, self.now)

    def positions(self) -> list[Position]:
        return list(self.book.positions.values())

    def collect_fills(self) -> list[Fill]:
        out, self.events = self.events, []
        return out

    def set_protection(self, sym: str, stop_loss: float | None, take_profit: float | None) -> None:
        pos = self.book.positions.get(sym)
        if pos is not None:
            pos.stop_loss, pos.take_profit = stop_loss, take_profit

    # --- persistence ---------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "book": self.book.to_dict(),
            "orders": {k: v.to_dict() for k, v in self.orders.items()},
            "closed_positions": self.closed_positions[-500:],
        }

    def load(self, d: dict[str, Any]) -> None:
        if d.get("version") != 1:
            raise ValueError("unsupported simulated broker state version")
        self.book = Book.from_dict(d["book"])
        self.orders = {}
        for k, o in d["orders"].items():
            self.orders[k] = OrderStatus(
                o["client_order_id"],
                o["broker_order_id"],
                o["pair"],
                Side(o["side"]),
                float(o["units"]),
                OrderState(o["state"]),
                float(o["filled_units"]),
                o["avg_price"],
                o["reason"],
            )
        self.closed_positions = list(d.get("closed_positions", []))
