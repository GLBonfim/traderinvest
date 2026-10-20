"""Round trips, restart, and interference detection with no virtual FX portfolio."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS  # noqa: N814

import pytest

from app.fx.ibkr.cash_reconciliation import CashReconciler, compare_cash
from app.fx.ibkr.paper import NotPaperAccountError
from app.fx.orders import ExecutionError
from app.fx.store import FxStore


class Broker:
    def __init__(self) -> None:
        self.account = "DU12314"
        self.cash = {"EUR": 996991.1025, "USD": 1121.25, "GBP": 841.65, "JPY": 177090.0}
        self.fills: list = []
        self.orders: list = []
        self.positions: list = []

    def managedAccounts(self) -> list[str]:  # noqa: N802
        return [self.account]

    def accountValues(self) -> list:  # noqa: N802
        return [
            NS(tag="$LEDGER-CashBalance", currency=c, value=str(v)) for c, v in self.cash.items()
        ]

    async def reqAllOpenOrdersAsync(self) -> list:  # noqa: N802
        return self.orders

    async def reqPositionsAsync(self) -> list:  # noqa: N802
        return self.positions

    async def reqExecutionsAsync(self, _filter: object) -> list:  # noqa: N802
        return self.fills


def fill(ref: str = "P-test", side: str = "SLD", fid: str = "new1", fee: float = 1.7795) -> NS:
    return NS(
        contract=NS(symbol="EUR", currency="USD", secType="CASH"),
        execution=NS(
            execId=fid, acctNumber="DU12314", orderRef=ref, side=side, shares=1000, price=1.12
        ),
        commissionReport=NS(execId=fid, currency="EUR", commission=fee),
    )


def setup(tmp_path: Path) -> tuple[CashReconciler, Broker, FxStore]:
    ib = Broker()
    funding = tmp_path / "funding.json"
    funding.write_text('{"state":"complete"}')
    anchor = tmp_path / "cash-anchor.json"
    anchor.write_text(
        json.dumps(
            {
                "schema": "fx-paper-cash-anchor/1",
                "account": ib.account,
                "cash": ib.cash,
                "prior_exec_ids": ["previous"],
                "funding_sha256": hashlib.sha256(funding.read_bytes()).hexdigest(),
            }
        )
    )
    store = FxStore(tmp_path / "pilot.sqlite")
    store.append("pilot_order_sent", {"coid": "P-test"})
    return CashReconciler(ib, store, anchor, funding), ib, store


def test_no_virtual_positions_and_known_prior_execution(tmp_path: Path) -> None:
    guard, ib, _ = setup(tmp_path)
    ib.fills = [fill(ref="old-manual", fid="previous")]
    assert asyncio.run(guard()) == {}


def test_entry_protective_exit_restart_and_deduplication(tmp_path: Path) -> None:
    guard, ib, store = setup(tmp_path)
    ib.fills = [fill()]
    ib.cash["EUR"] -= 1001.7795
    ib.cash["USD"] += 1120
    assert asyncio.run(guard()) == {"EURUSD": -1000}
    count = store.count()
    assert asyncio.run(guard()) == {"EURUSD": -1000}
    assert store.count() == count
    guard = CashReconciler(ib, store, tmp_path / "cash-anchor.json", tmp_path / "funding.json")
    assert asyncio.run(guard()) == {"EURUSD": -1000}
    ib.fills.append(fill(ref="P-test:sl", side="BOT", fid="new2"))
    ib.cash["EUR"] += 1000 - 1.7795
    ib.cash["USD"] -= 1120
    assert asyncio.run(guard()) == {}
    assert store.verify()[0]


@pytest.mark.parametrize(
    "kind", ["external_fill", "unknown_order", "non_fx", "bad_fee", "correction", "real_account"]
)
def test_interference_blocks(tmp_path: Path, kind: str) -> None:
    guard, ib, _ = setup(tmp_path)
    if kind == "external_fill":
        ib.fills = [fill(ref="manual")]
    elif kind == "unknown_order":
        ib.orders = [NS(order=NS(orderRef="external"))]
    elif kind == "non_fx":
        ib.positions = [NS(position=1, contract=NS(secType="STK"))]
    elif kind == "bad_fee":
        ib.fills = [fill(fee=float("inf"))]
    elif kind == "correction":
        guard.apply_fills([fill()])
        ib.fills = [fill(fee=2)]
    else:
        ib.account = "U12314"
    with pytest.raises((ExecutionError, NotPaperAccountError)):
        asyncio.run(guard())


def test_cash_change_without_execution_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, ib, _ = setup(tmp_path)
    ib.cash["USD"] += 100

    async def no_delay(_: float) -> None:
        pass

    monkeypatch.setattr("app.fx.ibkr.cash_reconciliation.asyncio.sleep", no_delay)
    with pytest.raises(ExecutionError, match="cash does not match"):
        asyncio.run(guard())


def test_anchor_cannot_change_on_restart(tmp_path: Path) -> None:
    guard, ib, store = setup(tmp_path)
    guard.apply_fills([fill()])
    anchor = tmp_path / "cash-anchor.json"
    anchor.write_text(anchor.read_text() + " ")
    with pytest.raises(ExecutionError, match="anchor changed"):
        CashReconciler(ib, store, anchor, tmp_path / "funding.json")


def test_cash_requires_all_currencies_and_rejects_extra_cash() -> None:
    expected = {"EUR": 100, "USD": 1, "GBP": 2, "JPY": 3}
    assert compare_cash(expected, expected)
    assert not compare_cash(expected, {"EUR": 100})
    assert not compare_cash(expected, expected | {"CHF": 100})


def test_partial_exit_and_commission_in_quote_currency(tmp_path: Path) -> None:
    guard, ib, _ = setup(tmp_path)
    entry = fill()
    entry.commissionReport.currency = "USD"
    ib.fills = [entry]
    ib.cash["EUR"] -= 1000
    ib.cash["USD"] += 1120 - 1.7795
    assert asyncio.run(guard()) == {"EURUSD": -1000}
    close = fill(ref="P-test:tp", side="BOT", fid="partial")
    close.execution.shares = 400
    ib.fills.append(close)
    ib.cash["EUR"] += 400 - 1.7795
    ib.cash["USD"] -= 448
    assert asyncio.run(guard()) == {"EURUSD": -600}


def test_corrupt_saved_state_cannot_be_adopted(tmp_path: Path) -> None:
    _, ib, store = setup(tmp_path)
    state = store.get_state("cash_reconciliation")
    state["cash"]["USD"] += 100
    store.put_state("cash_reconciliation", state)
    with pytest.raises(ExecutionError, match="state differs"):
        CashReconciler(ib, store, tmp_path / "cash-anchor.json", tmp_path / "funding.json")


def test_manual_integration_is_tracked_separately(tmp_path: Path) -> None:
    guard, ib, store = setup(tmp_path)
    store.append("user_demo_test_intent", {"ref": "USER-DEMO-new"})
    ib.fills = [fill(ref="USER-DEMO-new")]
    ib.cash["EUR"] -= 1001.7795
    ib.cash["USD"] += 1120
    assert asyncio.run(guard()) == {"EURUSD": -1000}
    assert not store.recent(1, ("pilot_order_filled", "pilot_close"))


def test_req_execution_copies_use_confirmed_public_commission_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard, ib, _ = setup(tmp_path)
    returned = fill()
    returned.commissionReport = NS(execId="", currency="", commission=0)
    cached = fill()

    async def request(_: object) -> list:
        return [returned]

    monkeypatch.setattr(ib, "reqExecutionsAsync", request)
    monkeypatch.setattr(ib, "fills", lambda: [cached])
    ib.cash["EUR"] -= 1001.7795
    ib.cash["USD"] += 1120
    assert asyncio.run(guard()) == {"EURUSD": -1000}
