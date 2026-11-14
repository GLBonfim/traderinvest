"""IBKR phase A (read-only, paper-only) against an in-memory fake of `ib_async.IB`.

No network, no Gateway, no credentials. The fake fails the test if anything tries to place,
modify or cancel an order.
"""

# ruff: noqa: N802, N803 - the fake mirrors ib_async's camelCase API

import asyncio
import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.fx.config import FxConfig
from app.fx.environment import ENABLED, Environment
from app.fx.ibkr.adapter import IbkrReadOnlyAdapter
from app.fx.ibkr.paper import NotPaperAccountError, assert_paper_accounts, mask
from app.fx.ibkr.probe import CANNOT_CONNECT, NOT_PAPER, OK, run
from app.fx.ibkr.settings import IbkrSettings
from app.fx.models import DataOrigin

NOW = datetime(2026, 10, 19, 13, 0, tzinfo=UTC)
NAN = float("nan")


def contract(sym: str, sec: str = "CASH", con_id: int = 1) -> Any:
    return SimpleNamespace(symbol=sym[:3], currency=sym[3:], secType=sec, conId=con_id)


class FakeIB:
    """The read-only subset of ib_async.IB, in memory."""

    def __init__(
        self,
        accounts: list[str] | None = None,
        refuse: bool = False,
        tickers: dict[str, tuple[float, float, int]] | None = None,
    ) -> None:
        self.accounts = ["DU1234567"] if accounts is None else accounts
        self.refuse = refuse
        self.tickers = tickers or {
            "EURUSD": (1.08341, 1.08349, 1),
            "USDJPY": (150.123, 150.131, 1),
            "GBPUSD": (NAN, 1.27, 1),  # no bid: must be reported missing
        }
        self.connected = False
        self.connect_args: dict[str, Any] = {}
        self.disconnects = 0
        self.mdt: int | None = None
        self.hist_args: tuple[Any, ...] = ()
        self.closed_until_frozen = False
        self.mdt_history: list[int] = []

    async def connectAsync(
        self,
        host: str,
        port: int,
        clientId: int,
        timeout: float | None,
        readonly: bool,
    ) -> None:
        if self.refuse:
            raise ConnectionRefusedError("no gateway")
        self.connected = True
        self.connect_args = {
            "host": host,
            "port": port,
            "client_id": clientId,
            "readonly": readonly,
        }

    def isConnected(self) -> bool:
        return self.connected

    def disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1

    def managedAccounts(self) -> list[str]:
        return self.accounts

    def reqMarketDataType(self, marketDataType: int) -> None:
        self.mdt = marketDataType
        self.mdt_history.append(marketDataType)

    async def qualifyContractsAsync(self, *contracts: Any) -> list[Any]:
        return list(contracts)

    async def reqTickersAsync(self, *contracts: Any) -> list[Any]:
        if self.closed_until_frozen and self.mdt != 2:
            return [
                SimpleNamespace(contract=c, bid=NAN, ask=NAN, time=None, marketDataType=1)
                for c in contracts
            ]
        out = []
        for c in contracts:
            sym = f"{c.symbol}{c.currency}"
            if sym in self.tickers:
                bid, ask, mdt = self.tickers[sym]
                out.append(
                    SimpleNamespace(contract=c, bid=bid, ask=ask, time=NOW, marketDataType=mdt)
                )
        return out

    async def reqHistoricalDataAsync(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.hist_args = (*args, kwargs)
        return [
            SimpleNamespace(
                date=NOW - timedelta(minutes=15 * (20 - i)),
                open=1.08 + i * 1e-4,
                high=1.081 + i * 1e-4,
                low=1.079,
                close=1.0805 + i * 1e-4,
            )
            for i in range(20)
        ]

    async def accountSummaryAsync(self, account: str = "") -> list[Any]:
        def v(tag: str, value: str, ccy: str = "EUR") -> Any:
            return SimpleNamespace(account=account, tag=tag, value=value, currency=ccy)

        return [
            v("NetLiquidation", "1000490.41"),
            v("TotalCashValue", "1000000.00"),
            v("UnrealizedPnL", "0"),
            v("InitMarginReq", "0"),
            v("AvailableFunds", "1000490.41"),
            v("AccountType", "INDIVIDUAL", ""),
        ]

    def positions(self, account: str = "") -> list[Any]:
        return [
            SimpleNamespace(contract=contract("EURUSD"), position=25000.0, avgCost=1.0811),
            SimpleNamespace(contract=contract("AAPLUSD", "STK"), position=10.0, avgCost=200.0),
            SimpleNamespace(contract=contract("USDTRY"), position=1000.0, avgCost=34.0),
        ]

    # anything that would trade fails the test
    def placeOrder(self, *a: Any, **k: Any) -> None:
        raise AssertionError("an order was attempted in phase A")

    def cancelOrder(self, *a: Any, **k: Any) -> None:
        raise AssertionError("a cancellation was attempted in phase A")


def adapter(fake: FakeIB) -> IbkrReadOnlyAdapter:
    return IbkrReadOnlyAdapter(IbkrSettings(), fake, clock=lambda: NOW)


def go(coro: Any) -> Any:
    return asyncio.run(coro)


# --- settings and paper guard ----------------------------------------------------------------


def test_settings_are_loopback_read_only_and_paper_only() -> None:
    s = IbkrSettings()
    assert (s.host, s.port, s.read_only, s.require_paper) == ("127.0.0.1", 4002, True, True)
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        IbkrSettings(host="192.168.1.10")
    with pytest.raises(ValueError):
        IbkrSettings(read_only=False)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        IbkrSettings(require_paper=False)  # type: ignore[arg-type]


def test_paper_guard() -> None:
    assert assert_paper_accounts(["DU1234567"]) == ["DU1234567"]
    for accounts in (["U1234567"], ["DU1234567", "U7654321"], [], [""]):
        with pytest.raises(NotPaperAccountError):
            assert_paper_accounts(accounts)
    assert mask("DU1234567") == "DU*****67"
    try:
        assert_paper_accounts(["U7654321"])
    except NotPaperAccountError as exc:
        assert "7654321" not in str(exc)  # never print a full account id


def test_engine_still_runs_simulation_only() -> None:
    cfg = FxConfig()
    assert cfg.environment is Environment.SIMULATION and {Environment.SIMULATION} == ENABLED
    assert cfg.ibkr.read_only and cfg.ibkr.require_paper


# --- connection ------------------------------------------------------------------------------


def test_connects_read_only_and_sets_market_data_type() -> None:
    fake = FakeIB()
    a = adapter(fake)
    assert go(a.connect()) == ["DU1234567"]
    assert fake.connect_args == {
        "host": "127.0.0.1",
        "port": 4002,
        "client_id": 17,
        "readonly": True,
    }
    assert fake.mdt == 1 and a.masked_accounts == ["DU*****67"]
    a.disconnect()
    assert not fake.connected


def test_live_session_is_refused_and_disconnected() -> None:
    fake = FakeIB(accounts=["U1234567"])
    with pytest.raises(NotPaperAccountError):
        go(adapter(fake).connect())
    assert fake.disconnects == 1 and not fake.connected and fake.mdt is None


def test_adapter_has_no_order_methods() -> None:
    a = adapter(FakeIB())
    for name in ("submit", "order", "cancel", "place_order", "placeOrder", "modify"):
        assert not hasattr(a, name)
    caps = a.capabilities
    assert caps.environment is Environment.DEMO and not caps.authoritative_order_lookup


# --- data mapping ----------------------------------------------------------------------------


def test_quotes_origin_and_missing_sides() -> None:
    fake = FakeIB(
        tickers={
            "EURUSD": (1.08341, 1.08349, 1),
            "USDJPY": (150.12, 150.13, 3),
            "GBPUSD": (NAN, 1.27, 1),
            "EURGBP": (0.86, 0.85, 1),
        }
    )
    a = adapter(fake)
    go(a.connect())
    rep = go(a.quotes(["EURUSD", "USDJPY", "GBPUSD", "EURGBP", "XAUUSD"]))
    assert rep.quotes["EURUSD"].origin is DataOrigin.LIVE
    assert rep.quotes["USDJPY"].origin is DataOrigin.DELAYED
    assert rep.quotes["EURUSD"].bid == 1.08341 and rep.quotes["EURUSD"].ts == NOW
    assert set(rep.missing) == {"GBPUSD", "EURGBP", "XAUUSD"}  # no bid / crossed / unknown


def test_bars_are_midpoint_utc_and_trimmed() -> None:
    fake = FakeIB()
    a = adapter(fake)
    go(a.connect())
    bars = go(a.bars("EURUSD", "M15", 8))
    assert len(bars) == 8 and all(b.origin is DataOrigin.HISTORICAL for b in bars)
    assert bars[-1].ts == NOW - timedelta(minutes=15) and bars[-1].ts.tzinfo is not None
    _contract, end, duration, size, what, rth, kw = fake.hist_args
    assert (end, size, what, rth, kw) == ("", "15 mins", "MIDPOINT", False, {"formatDate": 2})
    assert duration.endswith(" D")
    with pytest.raises(ValueError):
        go(a.bars("EURUSD", "D1", 5))


def test_account_and_positions() -> None:
    a = adapter(FakeIB())
    go(a.connect())
    acct = go(a.account())
    assert (acct.currency, acct.equity, acct.balance) == ("EUR", 1000490.41, 1000000.0)
    assert acct.margin_used == 0.0 and acct.free_margin == 1000490.41
    pos = a.positions()
    assert [(p.pair, p.units, p.avg_price) for p in pos] == [("EURUSD", 25000.0, 1.0811)]


# --- probe -----------------------------------------------------------------------------------


def probe(fake: FakeIB, now: datetime = NOW) -> tuple[int, str]:
    out = io.StringIO()
    code = go(
        run(
            IbkrSettings(),
            ["EURUSD", "USDJPY", "GBPUSD"],
            "EURUSD",
            client_factory=lambda: fake,
            out=out,
            clock=lambda: now,
        )
    )
    return code, out.getvalue()


def test_probe_with_the_market_closed_uses_frozen_quotes_then_restores_live() -> None:
    sunday = datetime(2026, 10, 18, 1, 45, tzinfo=UTC)  # FX closed until 21:00 UTC
    fake = FakeIB(tickers={"EURUSD": (1.12370, 1.12390, 2)})
    fake.closed_until_frozen = True
    code, text = probe(fake, sunday)
    assert code == OK
    assert "FECHADO até 2026-10-18 21:00Z" in text
    assert "congeladas" in text and "EURUSD bid 1.12370 ask 1.12390" in text
    assert fake.mdt_history == [1, 2, 1]  # live, frozen for the retry, live again


def test_probe_open_market_missing_quotes_points_to_permissions() -> None:
    fake = FakeIB(tickers={"EURUSD": (NAN, NAN, 1)})
    _code, text = probe(fake)
    assert "Mercado Forex: aberto" in text and "permissão Forex" in text


def test_probe_reads_everything_and_disconnects() -> None:
    fake = FakeIB()
    code, text = probe(fake)
    assert code == OK
    assert "DU*****67" in text and "DU1234567" not in text
    assert "EURUSD bid 1.08341 ask 1.08349" in text and "sem bid/ask: GBPUSD" in text
    assert "Posições Forex: 1" in text and "1,000,490.41 EUR" in text
    assert not fake.connected


def test_probe_refuses_live_and_reports_missing_gateway() -> None:
    code, text = probe(FakeIB(accounts=["U9999999"]))
    assert code == NOT_PAPER and "RECUSADO" in text
    code, text = probe(FakeIB(refuse=True))
    assert code == CANNOT_CONNECT and "Gateway" in text
