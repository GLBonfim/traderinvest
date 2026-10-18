"""Read-only IBKR adapter (phase A): quotes, bars, account and positions from a PAPER session.

Talks to IB Gateway/TWS through an `ib_async.IB`-like client, injected so tests use a fake
(no network). It has NO order methods: `submit`, `order` and cancellation do not exist here.
`connect()` connects with `readonly=True` and refuses (and disconnects from) any session whose
managed accounts are not paper accounts.

Mapping notes:
- quotes: IDEALPRO bid/ask; origin LIVE for market data type 1, DELAYED for 2-4 (frozen or
  delayed), missing sides (NaN) are skipped and reported;
- bars: MIDPOINT bars (useRTH=False, UTC). MIDPOINT carries no spread, so `Bar.spread` is
  0.0 and the origin is HISTORICAL;
- account: NetLiquidation (equity), TotalCashValue (balance), UnrealizedPnL,
  InitMarginReq (margin used), AvailableFunds (free margin), in the base currency;
- positions: CASH (forex) positions of known pairs; IBKR's avgCost is the average price.
"""

# ruff: noqa: N802, N803 - the client Protocol mirrors ib_async's camelCase API

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.fx.environment import Environment
from app.fx.ibkr.paper import assert_paper_accounts, mask
from app.fx.ibkr.settings import IbkrSettings
from app.fx.models import PAIRS, Bar, DataOrigin, Quote, pair
from app.fx.orders import AccountSnapshot, Position
from app.fx.ports import Capabilities

SOURCE = "ibkr-paper"
BAR_SIZES = {"M1": "1 min", "M5": "5 mins", "M15": "15 mins", "H1": "1 hour"}
BAR_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "H1": 60}


class IbkrClient(Protocol):
    """The subset of `ib_async.IB` this adapter uses (all read-only)."""

    async def connectAsync(
        self,
        host: str,
        port: int,
        clientId: int,
        timeout: float | None,
        readonly: bool,
    ) -> Any: ...

    def isConnected(self) -> bool: ...

    def disconnect(self) -> Any: ...

    def managedAccounts(self) -> list[str]: ...

    def reqMarketDataType(self, marketDataType: int) -> Any: ...

    async def qualifyContractsAsync(self, *contracts: Any) -> list[Any]: ...

    async def reqTickersAsync(self, *contracts: Any) -> list[Any]: ...

    async def reqHistoricalDataAsync(
        self,
        contract: Any,
        endDateTime: Any,
        durationStr: str,
        barSizeSetting: str,
        whatToShow: str,
        useRTH: bool,
        formatDate: int = 1,
    ) -> list[Any]: ...

    async def accountSummaryAsync(self, account: str = "") -> list[Any]: ...

    def positions(self, account: str = "") -> list[Any]: ...


def forex_contract(symbol: str) -> Any:
    from ib_async import Forex  # imported lazily: the simulator never needs ib_async

    return Forex(symbol)


@dataclass(frozen=True)
class QuoteReport:
    quotes: dict[str, Quote]
    missing: list[str]  # pairs without a usable bid/ask


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or v <= 0 else v


def _utc(ts: Any, fallback: datetime) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return fallback


class IbkrReadOnlyAdapter:
    """Phase A: read-only, paper-only. There is deliberately no way to send an order."""

    def __init__(
        self,
        settings: IbkrSettings,
        client: IbkrClient,
        contract_factory: Callable[[str], Any] = forex_contract,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.client = client
        self.contract_factory = contract_factory
        self.clock = clock
        self.accounts: list[str] = []

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            environment=Environment.DEMO,
            data_origin=DataOrigin.LIVE,
            server_side_stops=True,
            partial_fills=True,
            hedging=False,
            min_units=1.0,
            unit_step=1.0,
            max_leverage=30.0,
            description="IBKR conta paper — só leitura (fase A, sem envio de ordens)",
            authoritative_order_lookup=False,  # a remote venue: an empty lookup proves nothing
        )

    @property
    def account_id(self) -> str:
        return self.accounts[0] if self.accounts else ""

    @property
    def masked_accounts(self) -> list[str]:
        return [mask(a) for a in self.accounts]

    async def connect(self) -> list[str]:
        s = self.settings
        await self.client.connectAsync(
            s.host, s.port, s.client_id, s.connect_timeout_s, readonly=True
        )
        try:
            self.accounts = assert_paper_accounts(self.client.managedAccounts())
        except Exception:
            self.client.disconnect()  # never keep a session that is not paper
            raise
        self.client.reqMarketDataType(s.market_data_type)
        return self.accounts

    def set_market_data_type(self, market_data_type: int) -> None:
        """1 live, 2 frozen (last quote while the market is closed), 3 delayed, 4 both."""
        if market_data_type not in (1, 2, 3, 4):
            raise ValueError("market data type must be 1, 2, 3 or 4")
        self.client.reqMarketDataType(market_data_type)

    def disconnect(self) -> None:
        if self.client.isConnected():
            self.client.disconnect()

    # --- market data --------------------------------------------------------------------

    async def quotes(self, symbols: Sequence[str]) -> QuoteReport:
        known = [s for s in symbols if s in PAIRS]
        contracts = await self.client.qualifyContractsAsync(
            *[self.contract_factory(s) for s in known]
        )
        tickers = await self.client.reqTickersAsync(*[c for c in contracts if c is not None])
        now = self.clock()
        out: dict[str, Quote] = {}
        for t in tickers:
            c = t.contract
            sym = f"{c.symbol}{c.currency}"
            bid, ask = _num(t.bid), _num(t.ask)
            if sym not in PAIRS or bid is None or ask is None or ask < bid:
                continue
            origin = DataOrigin.LIVE if t.marketDataType == 1 else DataOrigin.DELAYED
            out[sym] = Quote(sym, bid, ask, _utc(t.time, now), origin, SOURCE)
        missing = [s for s in symbols if s not in out]
        return QuoteReport(out, missing)

    async def bars(self, symbol: str, timeframe: str, count: int) -> list[Bar]:
        if timeframe not in BAR_SIZES:
            raise ValueError(f"unsupported timeframe {timeframe!r}")
        pair(symbol)  # validates the symbol
        minutes = BAR_MINUTES[timeframe] * count
        days = max(2, math.ceil(minutes / 1440) * 2 + 2)  # cover weekends and holidays
        contract = self.contract_factory(symbol)
        rows = await self.client.reqHistoricalDataAsync(
            contract, "", f"{days} D", BAR_SIZES[timeframe], "MIDPOINT", False, formatDate=2
        )
        now = self.clock()
        bars = [
            Bar(
                symbol,
                _utc(r.date, now),
                float(r.open),
                float(r.high),
                float(r.low),
                float(r.close),
                0.0,
                DataOrigin.HISTORICAL,
            )
            for r in rows
            if isinstance(r.date, datetime)
        ]
        return bars[-count:]

    # --- account and positions ----------------------------------------------------------

    async def account(self) -> AccountSnapshot:
        values = await self.client.accountSummaryAsync(self.account_id)
        by_tag: dict[str, tuple[float, str]] = {}
        for v in values:
            if v.account and self.account_id and v.account != self.account_id:
                continue
            num = None
            try:
                num = float(v.value)
            except (TypeError, ValueError):
                continue
            by_tag.setdefault(v.tag, (num, v.currency))
        if "NetLiquidation" not in by_tag:
            raise RuntimeError("IBKR account summary without NetLiquidation")
        equity, currency = by_tag["NetLiquidation"]

        def tag(name: str) -> float:
            return by_tag.get(name, (0.0, currency))[0]

        return AccountSnapshot(
            currency=currency,
            balance=tag("TotalCashValue"),
            unrealized=tag("UnrealizedPnL"),
            equity=equity,
            margin_used=tag("InitMarginReq"),
            free_margin=tag("AvailableFunds"),
            ts=self.clock(),
        )

    def positions(self) -> list[Position]:
        out: list[Position] = []
        now = self.clock()
        for p in self.client.positions(self.account_id):
            c = p.contract
            sym = f"{c.symbol}{c.currency}"
            if c.secType != "CASH" or sym not in PAIRS or not float(p.position):
                continue
            out.append(
                Position(
                    sym, float(p.position), float(p.avgCost), now, f"ibkr-{getattr(c, 'conId', 0)}"
                )
            )
        return out
