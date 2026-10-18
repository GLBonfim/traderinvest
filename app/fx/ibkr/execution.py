"""IBKR PAPER order execution (phase B, experimental pilot). Never real money.

Guarantees:
- before EVERY order: the session's managed accounts are verified to be paper accounts (DU...),
  otherwise nothing is sent;
- entries are brackets: a market parent plus a take-profit limit and a stop held by IBKR
  (OCA, GTC); the parent carries the client order id in `orderRef`, children `<id>:tp/:sl`;
  after the entry fills, `protection_ok` must confirm both children are working (a stop that
  IBKR rejected leaves an unprotected position: the caller must stop with an alarm);
- a close first cancels the children and waits for IBKR to confirm; if the stop or target
  filled in the meantime that fill is returned and NO closing order is sent; any uncertainty
  raises an ambiguous error (pause, never a blind send);
- the paper identity is checked again immediately before each `placeOrder`, and an entry whose
  decision (`created_at`) aged beyond `max_decision_age_s` during the lookups is not sent
  (closes are always allowed);
- idempotent: before sending, the client order id is looked up (session trades, completed
  orders, executions); an existing order is returned, never sent twice;
- a missing answer within the timeout raises `ExecutionError(ambiguous=True)`: the caller must
  reconcile with `find()` and must not resend;
- quotes used for decisions must be LIVE (market data type 1) with a real, recent ticker
  timestamp (no fallback timestamp);
- a Gateway in "Read-Only API" mode refuses orders: `ReadOnlyApiError` (the owner must change
  that setting manually; this code never tries to).
"""

# ruff: noqa: N802, N803 - the client Protocol mirrors ib_async's camelCase API

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.fx.accounting import Converter
from app.fx.ibkr.adapter import forex_contract
from app.fx.ibkr.paper import assert_paper_accounts
from app.fx.lifecycle import OrderState
from app.fx.models import DataOrigin, Quote, Side, pair
from app.fx.orders import ExecutionError, Fill, OrderRequest, OrderStatus

SOURCE = "ibkr-paper-live"
DONE_FILLED = "Filled"
DONE_DEAD = frozenset({"Cancelled", "ApiCancelled", "Inactive"})
WORKING = frozenset({"PreSubmitted", "Submitted"})
READ_ONLY_CODES = frozenset({321})
CHILD_PENDING = frozenset({"ApiPending", "PendingSubmit", "PendingCancel", "ApiUpdate"})


class ReadOnlyApiError(ExecutionError):
    def __init__(self, message: str) -> None:
        super().__init__(message, ambiguous=False)


class StaleQuoteError(RuntimeError):
    pass


class SendBlockedError(ExecutionError):
    """A caller guard vetoed the send right before `placeOrder`: nothing was sent."""

    def __init__(self, message: str) -> None:
        super().__init__(message, ambiguous=False)


class IbExecClient(Protocol):
    def managedAccounts(self) -> list[str]: ...

    def reqMarketDataType(self, marketDataType: int) -> Any: ...

    def reqTickersAsync(self, *contracts: Any) -> Awaitable[list[Any]]: ...

    def qualifyContractsAsync(self, *contracts: Any) -> Awaitable[list[Any]]: ...

    def reqContractDetailsAsync(self, contract: Any) -> Awaitable[list[Any]]: ...

    def placeOrder(self, contract: Any, order: Any) -> Any: ...

    def cancelOrder(self, order: Any) -> Any: ...

    def trades(self) -> list[Any]: ...

    def openTrades(self) -> list[Any]: ...

    def reqCompletedOrdersAsync(self, apiOnly: bool) -> Awaitable[list[Any]]: ...


@dataclass(frozen=True)
class OrderFactory:
    """Builds ib_async orders (injectable so tests need no ib_async objects)."""

    market: Callable[[str, float], Any]
    limit: Callable[[str, float, float], Any]
    stop: Callable[[str, float, float], Any]


def ib_order_factory() -> OrderFactory:
    from ib_async import LimitOrder, MarketOrder, StopOrder

    return OrderFactory(
        market=lambda action, qty: MarketOrder(action, qty),
        limit=lambda action, qty, px: LimitOrder(action, qty, px),
        stop=lambda action, qty, px: StopOrder(action, qty, px),
    )


def round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    digits = max(0, -math.floor(math.log10(tick)) + 1)
    return round(round(price / tick) * tick, digits)


def _raw_commission(fill: Any) -> tuple[float, str]:
    """(amount, currency) of a fill's commission report. IBKR reports an unknown commission
    as a huge sentinel, or the report arrives later: both count as 0 for now."""
    report = getattr(fill, "commissionReport", None)
    try:
        c = float(report.commission)  # type: ignore[union-attr]
    except (AttributeError, TypeError, ValueError):
        return 0.0, ""
    cur = str(getattr(report, "currency", "") or "")
    return (c if math.isfinite(c) and 0 <= c < 1e9 else 0.0), cur


def _side(action: str) -> Side:
    return Side.BUY if action.upper() == "BUY" else Side.SELL


class IbkrPaperExecutor:
    def __init__(
        self,
        client: IbExecClient,
        timeout_s: float = 15.0,
        quote_max_age_s: float = 10.0,
        orders: OrderFactory | None = None,
        contract_factory: Callable[[str], Any] = forex_contract,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        poll_s: float = 0.25,
        currency: str = "EUR",
        max_decision_age_s: float = 10.0,
    ) -> None:
        self.client = client
        self.timeout_s = timeout_s
        self.quote_max_age_s = quote_max_age_s
        self.orders = orders or ib_order_factory()
        self.contract_factory = contract_factory
        self.clock = clock
        self.poll_s = poll_s
        self._ticks: dict[str, float] = {}
        self.currency = currency
        self.max_decision_age_s = max_decision_age_s
        self._last_quotes: dict[str, Quote] = {}

    # --- identity and data -------------------------------------------------------------------

    def verify_paper(self) -> str:
        """The account orders go to; raises NotPaperAccountError unless every account is DU."""
        return assert_paper_accounts(self.client.managedAccounts())[0]

    async def live_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """LIVE quotes with a real recent ticker timestamp; stale or delayed ones are omitted."""
        self.client.reqMarketDataType(1)
        contracts = [self.contract_factory(s) for s in symbols]
        await self.client.qualifyContractsAsync(*contracts)
        tickers = await self.client.reqTickersAsync(*contracts)
        now = self.clock()
        out: dict[str, Quote] = {}
        for t in tickers:
            sym = f"{t.contract.symbol}{t.contract.currency}"
            ts = t.time
            try:
                bid, ask = float(t.bid), float(t.ask)
            except (TypeError, ValueError):
                continue
            if (
                t.marketDataType != 1
                or not isinstance(ts, datetime)
                or ts.tzinfo is None
                or not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid < ask)
                or abs((now - ts).total_seconds()) > self.quote_max_age_s
            ):
                continue
            out[sym] = Quote(sym, bid, ask, ts, DataOrigin.LIVE, SOURCE)
        self._last_quotes.update(out)
        return out

    def commission_in_account(self, fill: Any) -> float:
        """A fill's commission in the account currency, at the latest LIVE mid. Without a rate
        the raw amount is used (an estimate; for USD->EUR it overstates the cost)."""
        amount, cur = _raw_commission(fill)
        if amount == 0 or not cur or cur == self.currency:
            return amount
        try:
            return amount * Converter(self._last_quotes).rate(cur, self.currency)
        except LookupError:
            return amount

    async def min_tick(self, symbol: str) -> float:
        if symbol not in self._ticks:
            details = await self.client.reqContractDetailsAsync(self.contract_factory(symbol))
            tick = float(details[0].minTick) if details else 0.0
            self._ticks[symbol] = tick if tick > 0 else pair(symbol).pip_size / 10
        return self._ticks[symbol]

    # --- lookup ------------------------------------------------------------------------------

    def _status_of(self, trade: Any, coid: str) -> OrderStatus:
        st = trade.orderStatus
        o = trade.order
        status_text = str(st.status)
        total = float(o.totalQuantity)
        filled = float(st.filled or 0.0)
        if status_text == DONE_FILLED:
            state = OrderState.FILLED
        elif status_text in DONE_DEAD:
            state = OrderState.CANCELLED if filled > 0 else OrderState.REJECTED
        elif filled > 0:
            state = OrderState.PARTIALLY_FILLED
        elif status_text in WORKING:
            state = OrderState.ACCEPTED
        else:
            state = OrderState.UNKNOWN
        fills = []
        for f in getattr(trade, "fills", []) or []:
            ex = f.execution
            fills.append(
                Fill(
                    str(ex.execId),
                    coid,
                    f"{trade.contract.symbol}{trade.contract.currency}",
                    _side(o.action),  # executions say BOT/SLD; the order's side is the same
                    float(ex.shares),
                    float(ex.price),
                    ex.time if isinstance(ex.time, datetime) else self.clock(),
                    self.commission_in_account(f),
                    0.0,
                    "order",
                )
            )
        reason = "; ".join(
            str(e.message) for e in (getattr(trade, "log", []) or []) if getattr(e, "message", "")
        )
        avg = float(st.avgFillPrice) if filled > 0 else None
        return OrderStatus(
            coid,
            str(o.permId or o.orderId),
            f"{trade.contract.symbol}{trade.contract.currency}",
            _side(o.action),
            total,
            state,
            filled,
            avg,
            reason,
            fills,
        )

    async def find(self, client_order_id: str) -> OrderStatus | None:
        """Session trades, then completed orders. None = not found by THIS lookup only."""
        for t in self.client.trades():
            if getattr(t.order, "orderRef", "") == client_order_id:
                return self._status_of(t, client_order_id)
        for t in await self.client.reqCompletedOrdersAsync(apiOnly=True):
            if getattr(t.order, "orderRef", "") == client_order_id:
                return self._status_of(t, client_order_id)
        return None

    def _read_only(self, trade: Any) -> bool:
        for e in getattr(trade, "log", []) or []:
            code = getattr(e, "errorCode", 0)
            msg = str(getattr(e, "message", "")).lower()
            if code in READ_ONLY_CODES or "read-only" in msg or "read only" in msg:
                return True
        return False

    async def _await_parent(self, trade: Any, coid: str) -> OrderStatus:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        while True:
            if self._read_only(trade):
                raise ReadOnlyApiError(
                    "o Gateway está em modo Read-Only API: desligue-o manualmente para o piloto"
                )
            status = self._status_of(trade, coid)
            if status.state in (OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED):
                return status
            if loop.time() >= deadline:
                if status.state in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED):
                    return status
                raise ExecutionError(f"sem resposta do IBKR para {coid}", ambiguous=True)
            await asyncio.sleep(self.poll_s)

    # --- orders ------------------------------------------------------------------------------

    async def place_bracket(self, req: OrderRequest) -> OrderStatus:
        """Market entry + take profit + stop (OCA). Requires a stop and a target."""
        if req.stop_loss is None or req.take_profit is None or req.reduce_only:
            raise ExecutionError("a pilot entry needs a stop and a target", ambiguous=False)
        self.verify_paper()
        existing = await self.find(req.client_order_id)
        if existing is not None:
            return existing  # idempotent: never a second order for the same id
        contract = self.contract_factory(req.pair)
        await self.client.qualifyContractsAsync(contract)
        tick = await self.min_tick(req.pair)
        action = "BUY" if req.side is Side.BUY else "SELL"
        exit_action = "SELL" if action == "BUY" else "BUY"
        qty = float(req.units)
        parent = self.orders.market(action, qty)
        tp = self.orders.limit(exit_action, qty, round_to_tick(req.take_profit, tick))
        sl = self.orders.stop(exit_action, qty, round_to_tick(req.stop_loss, tick))
        for o, ref in (
            (parent, req.client_order_id),
            (tp, f"{req.client_order_id}:tp"),
            (sl, f"{req.client_order_id}:sl"),
        ):
            o.orderRef = ref
        parent.transmit = False
        parent.tif = "DAY"
        for child in (tp, sl):
            child.tif = "GTC"
            child.ocaGroup = f"oca-{req.client_order_id}"
            child.ocaType = 1
        account = self.verify_paper()  # again, right before sending: no await in between
        age = (self.clock() - req.created_at).total_seconds()
        if age > self.max_decision_age_s:  # the lookups above took too long: stale decision
            raise ExecutionError(
                f"decisão de entrada com {age:.0f}s (máx {self.max_decision_age_s:.0f}s): "
                "não enviada",
                ambiguous=False,
            )
        for o in (parent, tp, sl):
            o.account = account
        trade = self.client.placeOrder(contract, parent)
        tp.parentId = sl.parentId = trade.order.orderId
        tp.transmit = False
        sl.transmit = True  # the last child transmits the whole bracket
        self.client.placeOrder(contract, tp)
        self.client.placeOrder(contract, sl)
        return await self._await_parent(trade, req.client_order_id)

    async def _await_done(self, trades: list[Any]) -> None:
        """Waits until IBKR reports every trade done (cancelled or filled)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        while any(str(t.orderStatus.status) not in DONE_DEAD | {DONE_FILLED} for t in trades):
            if loop.time() >= deadline:
                raise ExecutionError("cancelamento das proteções sem confirmação", ambiguous=True)
            await asyncio.sleep(self.poll_s)

    async def cancel_entry_remainder(self, client_order_id: str) -> OrderStatus:
        """Cancels the unfilled rest of a partially filled entry and returns IBKR's final
        status (it may have filled more, or completely, meanwhile). No answer: ambiguous."""
        trade = next(
            (
                t
                for t in self.client.trades()
                if getattr(t.order, "orderRef", "") == client_order_id
            ),
            None,
        )
        if trade is None:
            raise ExecutionError(f"entrada {client_order_id} fora desta sessão", ambiguous=True)
        if str(trade.orderStatus.status) not in DONE_DEAD | {DONE_FILLED}:
            self.verify_paper()
            self.client.cancelOrder(trade.order)
        await self._await_done([trade])
        return self._status_of(trade, client_order_id)

    async def protection_ok(self, entry_id: str) -> tuple[bool, str]:
        """Both protective orders of a filled entry are working at IBKR (or one already filled).
        Waits for pending states up to the timeout; anything else is NOT protected."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_s
        refs = (f"{entry_id}:tp", f"{entry_id}:sl")
        while True:
            by_ref = {str(getattr(t.order, "orderRef", "")): t for t in self.client.trades()}
            states = {r: str(by_ref[r].orderStatus.status) if r in by_ref else "" for r in refs}
            if any(v == DONE_FILLED for v in states.values()):
                return True, "proteção já executada"
            if all(v in WORKING for v in states.values()):
                return True, "stop e alvo ativos no IBKR"
            bad = {r: v for r, v in states.items() if v in DONE_DEAD or v == "ValidationError"}
            if bad or loop.time() >= deadline:
                detail = ", ".join(f"{r}={v or 'ausente'}" for r, v in states.items())
                return False, f"proteções não confirmadas ({detail})"
            await asyncio.sleep(self.poll_s)

    async def close_position(
        self, symbol: str, units: float, entry_id: str, client_order_id: str
    ) -> OrderStatus:
        """Closes `units` (signed) of an entry at market, after cancelling its protective
        orders and waiting for IBKR to confirm. If a stop/target filled meanwhile, that fill
        is returned (its client_order_id ends in :sl/:tp) and no closing order is sent."""
        if units == 0:
            raise ExecutionError("nothing to close", ambiguous=False)
        self.verify_paper()
        existing = await self.find(client_order_id)
        if existing is not None:
            return existing
        refs = (f"{entry_id}:tp", f"{entry_id}:sl")
        children = [t for t in self.client.trades() if getattr(t.order, "orderRef", "") in refs]
        for t in children:
            if str(t.orderStatus.status) not in DONE_DEAD | {DONE_FILLED}:
                self.client.cancelOrder(t.order)
        await self._await_done(children)
        filled = await self.protective_fill(entry_id)
        if filled is not None:
            if filled.filled_units + 1e-9 < abs(units):
                raise ExecutionError(
                    f"proteção de {entry_id} preenchida parcialmente durante o fecho",
                    ambiguous=True,
                )
            return filled
        contract = self.contract_factory(symbol)
        await self.client.qualifyContractsAsync(contract)
        order = self.orders.market("SELL" if units > 0 else "BUY", abs(float(units)))
        order.orderRef, order.tif = client_order_id, "DAY"
        order.account = self.verify_paper()  # right before sending
        trade = self.client.placeOrder(contract, order)
        return await self._await_parent(trade, client_order_id)

    async def place_market(
        self,
        symbol: str,
        side: Side,
        units: float,
        client_order_id: str,
        guard: Callable[[], None] | None = None,
    ) -> OrderStatus:
        """A plain DAY market order WITHOUT protection: only for explicit cash conversions
        (e.g. the paper lab's one-time currency buffers), never for strategy entries.
        Same guarantees: paper identity before sending, lookup first (never twice),
        ambiguous on timeout. `guard` runs immediately before `placeOrder` (after every
        await) and vetoes the send by raising SendBlockedError."""
        if units <= 0:
            raise ExecutionError("units must be > 0", ambiguous=False)
        self.verify_paper()
        existing = await self.find(client_order_id)
        if existing is not None:
            return existing
        contract = self.contract_factory(symbol)
        await self.client.qualifyContractsAsync(contract)
        order = self.orders.market("BUY" if side is Side.BUY else "SELL", float(units))
        order.orderRef, order.tif = client_order_id, "DAY"
        order.account = self.verify_paper()  # right before sending
        if guard is not None:
            guard()  # no await between this and placeOrder
        trade = self.client.placeOrder(contract, order)
        return await self._await_parent(trade, client_order_id)

    async def protective_fill(self, entry_id: str) -> OrderStatus | None:
        """The filled stop or target of an entry, if one filled."""
        for suffix in (":sl", ":tp"):
            st = await self.find(f"{entry_id}{suffix}")
            if st is not None and st.filled_units > 0:
                return st
        return None
