"""IBKR paper executor: identity, brackets, idempotency, ambiguity, protection, closes."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from app.fx.ibkr.execution import ReadOnlyApiError, round_to_tick
from app.fx.ibkr.paper import NotPaperAccountError
from app.fx.lifecycle import OrderState
from app.fx.models import DataOrigin, Side
from app.fx.orders import ExecutionError, OrderRequest
from tests.unit.fx.ibkr_fakes import NOW, FakeIB, executor, ticker


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def req(coid: str = "P-m-EURUSD-1", side: Side = Side.BUY) -> OrderRequest:
    stop, target = (1.0980, 1.1030) if side is Side.BUY else (1.1020, 1.0970)
    return OrderRequest(coid, coid, "EURUSD", side, 20_000, NOW, False, stop, target)


def session() -> FakeIB:
    ib = FakeIB()
    ib.tickers["EURUSD"] = ticker("EURUSD", 1.1000, 1.10006)
    return ib


def test_round_to_tick() -> None:
    assert round_to_tick(1.234567, 0.00005) == pytest.approx(1.23455)
    assert round_to_tick(151.2345, 0.005) == pytest.approx(151.235)


@pytest.mark.parametrize("accounts", [["U1234567"], ["DU1234567", "U7654321"], []])
def test_non_paper_session_sends_nothing(accounts: list[str]) -> None:
    ib = session()
    ib.accounts = accounts
    with pytest.raises(NotPaperAccountError):
        run(executor(ib).place_bracket(req()))
    assert ib.placed == []


def test_identity_rechecked_right_before_placing() -> None:
    ib = session()
    calls = iter([["DU1234567"], ["U1234567"]])  # the session changes during the awaits
    ib.accounts = lambda: next(calls)
    with pytest.raises(NotPaperAccountError):
        run(executor(ib).place_bracket(req()))
    assert ib.placed == []


def test_bracket_structure() -> None:
    ib = session()
    st = run(executor(ib).place_bracket(req()))
    assert st.state is OrderState.FILLED and st.filled_units == 20_000
    assert st.avg_price == pytest.approx(1.10006)
    parent, tp, sl = (o for _, o in ib.placed)
    assert (parent.orderType, parent.action, parent.transmit) == ("MKT", "BUY", False)
    assert (tp.orderType, tp.action, tp.lmtPrice) == ("LMT", "SELL", pytest.approx(1.103))
    assert (sl.orderType, sl.action, sl.auxPrice) == ("STP", "SELL", pytest.approx(1.098))
    assert tp.parentId == sl.parentId == parent.orderId
    assert tp.ocaGroup == sl.ocaGroup and tp.ocaType == 1 and tp.tif == sl.tif == "GTC"
    assert (tp.transmit, sl.transmit) == (False, True)
    assert [o.orderRef for _, o in ib.placed] == [
        "P-m-EURUSD-1",
        "P-m-EURUSD-1:tp",
        "P-m-EURUSD-1:sl",
    ]
    assert {o.account for _, o in ib.placed} == {"DU1234567"}
    assert st.fills[0].side is Side.BUY
    assert st.fills[0].commission == 2.0  # USD: no LIVE rate yet, raw amount (estimate)


def test_entry_without_stop_or_target_refused() -> None:
    ib = session()
    bad = OrderRequest("x", "x", "EURUSD", Side.BUY, 1000, NOW, False, None, 1.2)
    with pytest.raises(ExecutionError) as e:
        run(executor(ib).place_bracket(bad))
    assert not e.value.ambiguous and ib.placed == []


def test_same_client_order_id_never_sent_twice() -> None:
    ib = session()
    ex = executor(ib)
    run(ex.place_bracket(req()))
    again = run(ex.place_bracket(req()))
    assert len(ib.placed) == 3 and again.state is OrderState.FILLED


def test_completed_orders_also_count_for_idempotency() -> None:
    ib = session()
    run(executor(ib).place_bracket(req()))
    ib.completed, ib._trades = ib._trades, []  # e.g. after a restart of the session
    run(executor(ib).place_bracket(req()))
    assert len(ib.placed) == 3


def test_no_answer_is_ambiguous_and_not_resent() -> None:
    ib = session()
    ib.fill_market = False
    ex = executor(ib)
    with pytest.raises(ExecutionError) as e:
        run(ex.place_bracket(req()))
    assert e.value.ambiguous
    found = run(ex.find("P-m-EURUSD-1"))
    assert found is not None and found.state is OrderState.UNKNOWN
    again = run(ex.place_bracket(req()))  # the existing order is returned, nothing is sent
    assert again.state is OrderState.UNKNOWN and len(ib.placed) == 3


def test_read_only_gateway_is_reported_clearly() -> None:
    ib = session()
    ib.read_only = True
    with pytest.raises(ReadOnlyApiError) as e:
        run(executor(ib).place_bracket(req()))
    assert "Read-Only" in str(e.value) and not e.value.ambiguous


def test_live_quotes_require_live_type_and_real_recent_timestamp() -> None:
    ib = FakeIB()
    ib.tickers = {
        "EURUSD": ticker("EURUSD", 1.1, 1.10006),
        "GBPUSD": ticker("GBPUSD", 1.3, 1.3001, mdt=2),  # frozen
        "USDJPY": ticker("USDJPY", 150.0, 150.01, ts=None),  # no ticker timestamp
        "AUDUSD": ticker("AUDUSD", 0.66, 0.6601, ts=NOW - timedelta(seconds=60)),  # stale
        "USDCAD": ticker("USDCAD", 1.37, 1.36, mdt=1),  # crossed
    }
    q = run(executor(ib).live_quotes(["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"]))
    assert set(q) == {"EURUSD"}
    assert q["EURUSD"].origin is DataOrigin.LIVE and q["EURUSD"].ts == NOW
    assert ib.data_types == [1]


def test_protection_confirmed_or_alarm() -> None:
    ib = session()
    ex = executor(ib)
    run(ex.place_bracket(req()))
    assert run(ex.protection_ok("P-m-EURUSD-1"))[0]
    ib2 = session()
    ib2.child_status = "Inactive"  # e.g. IBKR rejected the stop
    ex2 = executor(ib2)
    assert run(ex2.place_bracket(req())).state is OrderState.FILLED
    ok, detail = run(ex2.protection_ok("P-m-EURUSD-1"))
    assert not ok and "Inactive" in detail


def test_close_cancels_protection_then_closes_at_market() -> None:
    ib = session()
    ex = executor(ib)
    run(ex.place_bracket(req()))
    st = run(ex.close_position("EURUSD", 20_000, "P-m-EURUSD-1", "P-m-EURUSD-1:x"))
    assert st.state is OrderState.FILLED and st.client_order_id == "P-m-EURUSD-1:x"
    assert len(ib.cancelled) == 2
    close = ib.placed[-1][1]
    assert (close.orderType, close.action, close.totalQuantity) == ("MKT", "SELL", 20_000)
    assert ib.positions["EURUSD"] == 0


def test_close_race_with_stop_returns_the_stop_and_sends_nothing() -> None:
    ib = session()
    ex = executor(ib)
    run(ex.place_bracket(req()))
    # the stop fills while we cancel it: the target is cancelled by OCA, no close is sent
    ib.on_cancel = lambda t: (
        ib.hit("P-m-EURUSD-1:sl")
        if t.order.orderRef.endswith(":tp") and t.orderStatus.status == "Submitted"
        else None
    )
    n = len(ib.placed)
    st = run(ex.close_position("EURUSD", 20_000, "P-m-EURUSD-1", "P-m-EURUSD-1:x"))
    assert st.client_order_id == "P-m-EURUSD-1:sl" and st.filled_units == 20_000
    assert len(ib.placed) == n and ib.positions["EURUSD"] == 0  # never reversed


def test_close_without_cancel_confirmation_pauses() -> None:
    ib = session()
    ex = executor(ib)
    run(ex.place_bracket(req()))
    ib.cancel_ack = False
    n = len(ib.placed)
    with pytest.raises(ExecutionError) as e:
        run(ex.close_position("EURUSD", 20_000, "P-m-EURUSD-1", "P-m-EURUSD-1:x"))
    assert e.value.ambiguous and len(ib.placed) == n


def test_commission_converted_to_account_currency() -> None:
    ib = session()
    ex = executor(ib)
    run(ex.live_quotes(["EURUSD"]))
    st = run(ex.place_bracket(req()))
    assert st.fills[0].commission == pytest.approx(2.0 / 1.10003)  # USD -> EUR at the mid


def test_partial_fill_is_reported_as_partial() -> None:
    ib = session()
    ib.partial = 5_000
    st = run(executor(ib).place_bracket(req()))
    assert st.state is OrderState.PARTIALLY_FILLED and st.filled_units == 5_000


def test_entry_decision_aged_during_lookups_is_not_sent() -> None:
    ib = session()
    now = [NOW]
    ex = executor(ib, clock=lambda: now[0])

    def slow() -> None:
        now[0] += timedelta(seconds=30)  # contract qualification took 30 s

    ib.on_qualify = slow
    with pytest.raises(ExecutionError) as e:
        run(ex.place_bracket(req()))
    assert not e.value.ambiguous and "não enviada" in str(e.value) and ib.placed == []


def test_close_still_allowed_after_slow_lookups() -> None:
    ib = session()
    now = [NOW]
    ex = executor(ib, clock=lambda: now[0])
    run(ex.place_bracket(req()))
    ib.on_qualify = lambda: now.__setitem__(0, now[0] + timedelta(seconds=30))
    st = run(ex.close_position("EURUSD", 20_000, "P-m-EURUSD-1", "P-m-EURUSD-1:x"))
    assert st.state is OrderState.FILLED and ib.positions["EURUSD"] == 0
