"""Multi-currency netting accounting and the simulated broker."""

from datetime import timedelta

import pytest

from app.fx.accounting import Book, ConversionUnavailableError, Converter
from app.fx.lifecycle import OrderState
from app.fx.models import Side
from app.fx.orders import ExecutionError, OrderRequest
from app.fx.sim.broker import SimBroker, SimBrokerConfig
from tests.unit.fx.helpers import NOW, quote, quotes


def test_converter_direct_inverse_and_cross() -> None:
    c = Converter(quotes())
    assert c.rate("EUR", "EUR") == 1.0
    assert c.rate("EUR", "USD") == pytest.approx(1.08)
    assert c.rate("USD", "EUR") == pytest.approx(1 / 1.08)
    assert c.rate("JPY", "EUR") == pytest.approx(1 / (150.0 * 1.08))  # via USD
    with pytest.raises(ConversionUnavailableError):
        Converter({}).rate("AUD", "USD")


def test_open_increase_reduce_close_and_reverse() -> None:
    conv = Converter(quotes())
    b = Book("EUR", 10_000.0, 30.0)
    b.apply_fill("EURUSD", Side.BUY, 10_000, 1.0800, NOW, 0.0, conv)
    b.apply_fill("EURUSD", Side.BUY, 10_000, 1.0820, NOW, 0.0, conv)
    pos = b.positions["EURUSD"]
    assert (pos.units, pos.avg_price) == (20_000, pytest.approx(1.0810))
    r = b.apply_fill("EURUSD", Side.SELL, 5_000, 1.0830, NOW, 1.0, conv)
    assert r.realized_quote == pytest.approx(5_000 * 0.0020)  # USD
    assert r.realized == pytest.approx(10.0 / 1.08)  # converted to EUR
    assert b.balance == pytest.approx(10_000 + 10.0 / 1.08 - 1.0)
    r = b.apply_fill("EURUSD", Side.SELL, 25_000, 1.0800, NOW, 0.0, conv)  # close 15k, short 10k
    assert r.closed_position is not None and r.opened
    assert b.positions["EURUSD"].units == -10_000
    assert b.positions["EURUSD"].avg_price == 1.0800
    b.apply_fill("EURUSD", Side.BUY, 10_000, 1.0790, NOW, 0.0, conv)
    assert "EURUSD" not in b.positions


def test_unrealized_marks_at_the_liquidation_side_and_margin() -> None:
    q = quotes(EURUSD=1.0800)
    b = Book("EUR", 10_000.0, 30.0)
    b.apply_fill("EURUSD", Side.BUY, 30_000, q["EURUSD"].ask, NOW, 0.0, Converter(q))
    # immediately after buying at the ask, marking at the bid shows the spread as a loss
    assert b.unrealized(q) == pytest.approx(-30_000 * q["EURUSD"].spread / 1.08)
    assert b.margin_used(q) == pytest.approx(30_000 / 30)  # EUR base, EUR account
    snap = b.snapshot(q, NOW)
    assert snap.equity == pytest.approx(b.balance + b.unrealized(q))
    assert snap.free_margin == pytest.approx(snap.equity - snap.margin_used)
    exp = b.currency_exposure(q)
    assert exp["EUR"] == pytest.approx(30_000) and exp["USD"] == pytest.approx(-30_000)


def test_jpy_pair_pnl_in_eur_account() -> None:
    q = quotes(USDJPY=150.0)
    b = Book("EUR", 10_000.0, 30.0)
    conv = Converter(q)
    b.apply_fill("USDJPY", Side.SELL, 10_000, 150.00, NOW, 0.0, conv)
    r = b.apply_fill("USDJPY", Side.BUY, 10_000, 149.00, NOW, 0.0, conv)
    assert r.realized_quote == pytest.approx(10_000.0)  # JPY
    assert r.realized == pytest.approx(10_000.0 / (150.0 * 1.08))


def test_book_roundtrip() -> None:
    b = Book("EUR", 10_000.0, 30.0)
    b.apply_fill("GBPUSD", Side.SELL, 7_000, 1.27, NOW, 0.5, Converter(quotes()), 1.28, 1.25)
    again = Book.from_dict(b.to_dict())
    assert again.to_dict() == b.to_dict()


# --- simulated broker --------------------------------------------------------------------


def broker(**cfg: float) -> SimBroker:
    br = SimBroker(SimBrokerConfig(slippage_pips=0.0, commission_per_million=0.0, **cfg))  # type: ignore[arg-type]
    br.on_quotes(quotes())
    return br


def req(coid: str, side: Side, units: float, **kw: object) -> OrderRequest:
    return OrderRequest(coid, "int-" + coid, "EURUSD", side, units, NOW, **kw)  # type: ignore[arg-type]


def test_market_fill_is_idempotent_by_client_order_id() -> None:
    br = broker()
    s1 = br.submit(req("a", Side.BUY, 10_000))
    s2 = br.submit(req("a", Side.BUY, 10_000))
    assert s1 is s2 and s1.state is OrderState.FILLED
    assert br.positions()[0].units == 10_000  # one order only
    assert s1.avg_price == quotes()["EURUSD"].ask


def test_injected_rejection_timeout_and_partial() -> None:
    br = broker()
    br.faults.reject_next = 1
    assert br.submit(req("r", Side.BUY, 10_000)).state is OrderState.REJECTED
    assert br.positions() == []
    br.faults.timeout_next = 1
    with pytest.raises(ExecutionError) as exc:
        br.submit(req("t", Side.BUY, 10_000))
    assert exc.value.ambiguous
    found = br.order("t")
    assert found is not None and found.state is OrderState.FILLED  # it did reach the venue
    br.faults.partial_next = 1
    st = br.submit(req("p", Side.BUY, 10_000))
    assert st.state is OrderState.PARTIALLY_FILLED and st.filled_units == 5_000


def test_reduce_only_and_margin_rejection() -> None:
    br = broker()
    assert br.submit(req("x", Side.SELL, 1_000, reduce_only=True)).state is OrderState.REJECTED
    big = br.submit(req("big", Side.BUY, 10_000_000))
    assert big.state is OrderState.REJECTED and "margin" in big.reason


def test_stop_loss_fills_at_the_gapped_quote_and_take_profit() -> None:
    br = broker()
    br.submit(req("l", Side.BUY, 10_000, stop_loss=1.0780, take_profit=1.0850))
    gap = quote("EURUSD", 1.0760, ts=NOW + timedelta(minutes=1))
    fills = br.on_quotes({"EURUSD": gap})
    assert [f.reason for f in fills] == ["stop_loss"]
    assert fills[0].price == round(gap.bid, 5)  # no guaranteed stop: the gap is paid
    assert br.positions() == []
    br.submit(req("s", Side.SELL, 10_000, stop_loss=1.0800, take_profit=1.0700))
    fills = br.on_quotes({"EURUSD": quote("EURUSD", 1.0690, ts=NOW + timedelta(minutes=2))})
    assert [f.reason for f in fills] == ["take_profit"]
    assert br.closed_positions[-1]["reason"] == "take_profit"


def test_margin_close_out() -> None:
    br = broker(initial_balance=1_000.0)
    br.submit(req("m", Side.BUY, 29_000))  # ~27k EUR notional, ~900 EUR margin on 1k equity
    fills = br.on_quotes({"EURUSD": quote("EURUSD", 1.0550, ts=NOW + timedelta(minutes=1))})
    assert [f.reason for f in fills] == ["stop_out"]
    assert br.positions() == []


def test_broker_state_roundtrip() -> None:
    br = broker()
    br.submit(req("a", Side.BUY, 10_000, stop_loss=1.07))
    again = SimBroker(br.config)
    again.load(br.to_dict())
    assert again.book.to_dict() == br.book.to_dict()
    restored = again.order("a")
    assert restored is not None and restored.state is OrderState.FILLED
