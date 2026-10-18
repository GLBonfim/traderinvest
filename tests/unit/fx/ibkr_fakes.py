"""A fake ib_async session for the paper execution/pilot tests (no network, no ib_async)."""

from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.fx.ibkr.execution import IbkrPaperExecutor, OrderFactory

NOW = datetime(2026, 10, 21, 14, 0, 30, tzinfo=UTC)  # Wednesday, London session


def contract(symbol: str) -> SimpleNamespace:
    return SimpleNamespace(symbol=symbol[:3], currency=symbol[3:], secType="CASH")


def _order(kind: str, action: str, qty: float, px: float | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        orderType=kind,
        action=action,
        totalQuantity=qty,
        lmtPrice=px if kind == "LMT" else None,
        auxPrice=px if kind == "STP" else None,
        orderId=0,
        permId=0,
        orderRef="",
        account="",
        transmit=True,
        tif="",
        ocaGroup="",
        ocaType=0,
        parentId=0,
    )


FACTORY = OrderFactory(
    market=lambda a, q: _order("MKT", a, q),
    limit=lambda a, q, p: _order("LMT", a, q, p),
    stop=lambda a, q, p: _order("STP", a, q, p),
)


def ticker(symbol: str, bid: float, ask: float, ts: Any = NOW, mdt: int = 1) -> SimpleNamespace:
    return SimpleNamespace(contract=contract(symbol), bid=bid, ask=ask, time=ts, marketDataType=mdt)


class FakeIB:
    """Fills market orders at the quote when a transmitting order arrives (like a bracket)."""

    def __init__(self, accounts: list[str] | None = None) -> None:
        self.accounts: list[str] | Callable[[], list[str]] = accounts or ["DU1234567"]
        self.tickers: dict[str, SimpleNamespace] = {}
        self.placed: list[tuple[Any, Any]] = []
        self.cancelled: list[Any] = []
        self._trades: list[SimpleNamespace] = []
        self.completed: list[SimpleNamespace] = []
        self.positions: dict[str, float] = {}
        self.next_id = 100
        self.exec_seq = 0
        self.fill_market = True  # False: the parent never gets an answer (ambiguous)
        self.partial: float | None = None  # fill only this many units of a market order
        self.child_status = "Submitted"  # status given to protective orders
        self.read_only = False
        self.on_cancel: Callable[[SimpleNamespace], None] | None = None
        self.cancel_ack = True
        self.data_types: list[int] = []
        self.on_qualify: Callable[[], None] | None = None

    # --- session --------------------------------------------------------------------------

    def managedAccounts(self) -> list[str]:  # noqa: N802
        a = self.accounts
        return a() if callable(a) else list(a)

    def reqMarketDataType(self, marketDataType: int) -> None:  # noqa: N802, N803
        self.data_types.append(marketDataType)

    async def reqTickersAsync(self, *contracts: Any) -> list[Any]:  # noqa: N802
        out = []
        for c in contracts:
            t = self.tickers.get(f"{c.symbol}{c.currency}")
            if t is not None:
                out.append(t)
        return out

    async def qualifyContractsAsync(self, *contracts: Any) -> list[Any]:  # noqa: N802
        if self.on_qualify is not None:
            self.on_qualify()
        return list(contracts)

    async def reqContractDetailsAsync(self, contract: Any) -> list[Any]:  # noqa: N802
        return [SimpleNamespace(minTick=0.00005)]

    # --- orders ---------------------------------------------------------------------------

    def _fill(self, trade: SimpleNamespace, price: float, qty: float | None = None) -> None:
        o = trade.order
        done = float(trade.orderStatus.filled or 0.0)
        qty = o.totalQuantity - done if qty is None else qty  # this fill's own units
        self.exec_seq += 1
        total = done + qty
        trade.orderStatus.status = "Filled" if total >= o.totalQuantity else "Submitted"
        trade.orderStatus.filled = total
        trade.orderStatus.avgFillPrice = price
        trade.fills.append(
            SimpleNamespace(
                execution=SimpleNamespace(
                    execId=f"E{self.exec_seq}",
                    side="BOT" if o.action == "BUY" else "SLD",
                    shares=qty,
                    price=price,
                    time=NOW,
                ),
                commissionReport=SimpleNamespace(commission=2.0, currency="USD"),
            )
        )
        sym = f"{trade.contract.symbol}{trade.contract.currency}"
        sign = 1 if o.action == "BUY" else -1
        self.positions[sym] = self.positions.get(sym, 0.0) + sign * qty

    def _price(self, trade: SimpleNamespace) -> float:
        t = self.tickers[f"{trade.contract.symbol}{trade.contract.currency}"]
        return float(t.ask if trade.order.action == "BUY" else t.bid)

    def placeOrder(self, contract: Any, order: Any) -> SimpleNamespace:  # noqa: N802
        self.next_id += 1
        order.orderId = order.orderId or self.next_id
        order.permId = 9000 + self.next_id
        trade = SimpleNamespace(
            contract=contract,
            order=order,
            orderStatus=SimpleNamespace(status="PendingSubmit", filled=0.0, avgFillPrice=0.0),
            fills=[],
            log=[],
        )
        self.placed.append((contract, order))
        self._trades.append(trade)
        if self.read_only:
            trade.orderStatus.status = "Cancelled"
            trade.log.append(SimpleNamespace(errorCode=321, message="API is in Read-Only mode"))
            return trade
        if order.parentId:
            trade.orderStatus.status = self.child_status
        if order.transmit:
            for t in self._trades:
                pending = t.order.orderType == "MKT" and t.orderStatus.status == "PendingSubmit"
                if pending and self.fill_market:
                    self._fill(t, self._price(t), self.partial)
        return trade

    def cancelOrder(self, order: Any) -> None:  # noqa: N802
        self.cancelled.append(order)
        for t in self._trades:
            if t.order is order:
                if self.on_cancel is not None:
                    self.on_cancel(t)
                elif self.cancel_ack:
                    t.orderStatus.status = "Cancelled"

    def trades(self) -> list[Any]:
        return list(self._trades)

    def openTrades(self) -> list[Any]:  # noqa: N802
        return [t for t in self._trades if t.orderStatus.status not in ("Filled", "Cancelled")]

    async def reqCompletedOrdersAsync(self, apiOnly: bool) -> list[Any]:  # noqa: N802, N803
        return list(self.completed)

    # --- helpers for tests ----------------------------------------------------------------

    def by_ref(self, ref: str) -> SimpleNamespace:
        return next(t for t in self._trades if t.order.orderRef == ref)

    def hit(self, ref: str) -> None:
        """The protective order `ref` executes (its OCA sibling is cancelled)."""
        t = self.by_ref(ref)
        self._fill(t, float(t.order.auxPrice or t.order.lmtPrice))
        for other in self._trades:
            if other.order.ocaGroup and other.order.ocaGroup == t.order.ocaGroup and other is not t:
                other.orderStatus.status = "Cancelled"


def executor(ib: FakeIB, clock: Callable[[], datetime] = lambda: NOW) -> IbkrPaperExecutor:
    return IbkrPaperExecutor(
        ib, timeout_s=0.05, poll_s=0.001, orders=FACTORY, contract_factory=contract, clock=clock
    )
