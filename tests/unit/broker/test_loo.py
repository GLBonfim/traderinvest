"""ADR-0027: limit-on-open buys bounded by the sandbox budget — exact limit prices (Decimal, Alpaca
increments), the default policy end to end through fake gateways and through the real adapter
over a simulated transport (no network): price within / above the limit, unfilled, partial,
rejected, duplicate, insufficient budget or cash, sells, rebalances, and the OPG window."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.broker.alpaca import AlpacaPaperGateway
from app.broker.config import OPG_LOO_BUYS, BrokerConfig
from app.broker.executor import target_quantity
from app.broker.gateway import BrokerOrder, BrokerSafetyError, OrderRequest
from app.broker.pricing import budget_limit_price, format_limit_price
from app.broker.reconcile import reconcile
from app.broker.timing import NEW_YORK
from tests.unit.broker.test_broker import (
    KEY,
    ORDER_JSON,
    SECRET,
    FakeGateway,
    FakeHTTP,
    account_with_pending_entry,
    executor,
    outcomes,
)

LOO = BrokerConfig()  # the default policy
EFFECTIVE = 9_999.0  # 10,000 budget - 1.00 cost allowance


def et(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=NEW_YORK)


# ── limit price arithmetic ──


def test_default_policy_is_limit_on_open_with_an_effective_budget() -> None:
    assert LOO.order_policy == OPG_LOO_BUYS
    assert (LOO.allocated_capital, LOO.order_cost_allowance, LOO.sizing_capital) == (
        10_000.0,
        1.0,
        EFFECTIVE,
    )
    with pytest.raises(ValueError, match="order_cost_allowance"):
        BrokerConfig(order_cost_allowance=10_000.0)
    with pytest.raises(ValueError, match="order_cost_allowance"):
        BrokerConfig(order_cost_allowance=-1.0)


def test_spy_pending_entry_numbers() -> None:
    """The real pending SPY entry: decision close 778.570007 (2026-10-09)."""
    qty = target_quantity(1.0, EFFECTIVE, 778.570007, OPG_LOO_BUYS)
    limit = budget_limit_price(EFFECTIVE, qty)
    assert (qty, limit) == (12.0, Decimal("833.25"))
    worst = Decimal(12) * limit
    assert worst == Decimal("9999.00") and worst + Decimal("1.00") <= Decimal("10000")
    assert limit >= Decimal("778.57")  # a flat open is inside the limit


def test_limit_price_is_the_highest_valid_price_within_the_budget() -> None:
    rng = np.random.default_rng(20261010)
    for _ in range(5_000):
        budget = round(float(rng.uniform(1, 50_000)), 2)
        shares = float(rng.integers(1, 501))
        try:
            p = budget_limit_price(budget, shares)
        except ValueError:
            assert budget / shares < 0.0001
            continue
        tick = Decimal("0.01") if p >= 1 else Decimal("0.0001")
        b, n = Decimal(str(budget)), Decimal(str(shares))
        assert n * p <= b  # never above the budget
        assert n * (p + tick) > b or (p < 1 <= p + tick)  # maximal on its increment
        assert p == p.quantize(tick) and format_limit_price(float(p)) == str(p)


def test_limit_price_increments_and_formatting() -> None:
    assert budget_limit_price(10, 13) == Decimal("0.7692")  # below 1.00: 4 decimals
    assert budget_limit_price(10_000, 3) == Decimal("3333.33")  # rounded DOWN, never up
    assert format_limit_price(5) == "5.00" and format_limit_price(0.1234) == "0.1234"
    for bad in (833.255, 0.12345, 0, -1):
        with pytest.raises(ValueError):
            format_limit_price(bad)
    with pytest.raises(ValueError):
        budget_limit_price(0.00004, 1)
    with pytest.raises(ValueError):
        budget_limit_price(100, 0)


# ── adapter guards and wire format (simulated transport) ──


def test_adapter_limit_payload_and_local_refusals() -> None:
    http = FakeHTTP()
    http.routes[("POST", "/v2/orders")] = (
        200,
        {**ORDER_JSON, "type": "limit", "limit_price": "833.25", "time_in_force": "opg"},
    )
    gw = AlpacaPaperGateway(KEY, SECRET, transport=http)
    o = gw.submit(OrderRequest("c", "SPY", "buy", 12.0, "opg", "limit", 833.25))
    assert (o.order_type, o.limit_price) == ("limit", 833.25)
    assert http.calls[-1][3] == {
        "symbol": "SPY",
        "qty": "12",
        "side": "buy",
        "type": "limit",
        "time_in_force": "opg",
        "client_order_id": "c",
        "limit_price": "833.25",
    }
    n = len(http.calls)
    for bad in (
        OrderRequest("c", "SPY", "buy", 12.0, "opg", "limit", None),  # no limit price
        OrderRequest("c", "SPY", "buy", 12.5, "opg", "limit", 800.0),  # fractional limit
        OrderRequest("c", "SPY", "buy", 12.0, "opg", "limit", 833.255),  # sub-penny
        OrderRequest("c", "SPY", "buy", 12.0, "opg", "market", 800.0),  # market with a price
        OrderRequest("c", "SPY", "buy", 12.0, "opg", "stop", None),  # unsupported type
    ):
        with pytest.raises(BrokerSafetyError):
            gw.submit(bad)
    assert len(http.calls) == n  # refused locally: nothing was sent


# ── executor with the default LOO policy (fixture: decision close 500, Tue 2026-10-06 open) ──


def test_dry_run_builds_a_budget_bounded_limit_on_open_buy(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    rep = executor(tmp_path, FakeGateway(), now, cfg=LOO).sync(dry_run=True)
    assert outcomes(rep) == ["would_submit"]
    order = rep.items[0].order
    assert order is not None
    assert (order["side"], order["qty"], order["order_type"], order["time_in_force"]) == (
        "buy",
        19.0,  # floor(9,999 / 500)
        "limit",
        "opg",
    )
    assert order["limit_price"] == 526.26  # floor_cent(9,999 / 19)
    assert Decimal("19") * Decimal(str(order["limit_price"])) + Decimal("1") <= Decimal("10000")


def test_fill_within_the_limit_is_clean(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    assert outcomes(executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)) == ["submitted"]
    req = gw.submits[0]
    assert (req.order_type, req.limit_price) == ("limit", 526.26)
    gw.orders[req.client_order_id] = replace(
        gw.orders[req.client_order_id], status="filled", filled_qty=19.0, filled_avg_price=510.0
    )
    ex = executor(tmp_path, gw, now, cfg=LOO)
    ex.sync(dry_run=False)
    rec = reconcile(LOO, ex.store, gw, tmp_path / "paper", now)  # type: ignore[arg-type]
    assert rec.budget_breaches == [] and rec.limit_violations == [] and rec.unfilled == []


def test_auction_above_the_limit_leaves_the_order_unfilled_and_nothing_is_retried(
    tmp_path: Path,
) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)
    cid = gw.submits[0].client_order_id
    # opening auction at 540 > limit 526.26: a limit-on-open is not filled and is cancelled
    gw.orders[cid] = replace(gw.orders[cid], status="canceled", filled_qty=0.0)
    ex = executor(tmp_path, gw, now, cfg=LOO)
    rep = ex.sync(dry_run=False)
    assert outcomes(rep) == ["already_submitted"] and len(gw.submits) == 1
    rec = reconcile(LOO, ex.store, gw, tmp_path / "paper", now)  # type: ignore[arg-type]
    assert rec.unfilled == [
        {
            "client_order_id": cid,
            "status": "canceled",
            "order_type": "limit",
            "limit_price": 526.26,
            "requested": 19.0,
        }
    ]
    assert rec.budget_breaches == [] and rec.partial_fills == []


def test_fill_above_the_limit_is_detected_never_corrected(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)
    cid = gw.submits[0].client_order_id
    # cannot happen for a buy limit; simulated to prove the detection
    gw.orders[cid] = replace(
        gw.orders[cid], status="filled", filled_qty=19.0, filled_avg_price=530.0
    )
    ex = executor(tmp_path, gw, now, cfg=LOO)
    ex.sync(dry_run=False)
    rec = reconcile(LOO, ex.store, gw, tmp_path / "paper", now)  # type: ignore[arg-type]
    assert rec.limit_violations == [
        {"client_order_id": cid, "filled_avg_price": 530.0, "limit": 526.26}
    ]
    assert rec.budget_breaches[0]["filled_cost"] == 19 * 530.0  # 10,070 > 10,000
    assert len(gw.submits) == 1


def test_partial_limit_on_open_fill(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)
    cid = gw.submits[0].client_order_id
    gw.orders[cid] = replace(
        gw.orders[cid], status="canceled", filled_qty=7.0, filled_avg_price=525.0
    )
    ex = executor(tmp_path, gw, now, cfg=LOO)
    ex.sync(dry_run=False)
    rec = reconcile(LOO, ex.store, gw, tmp_path / "paper", now)  # type: ignore[arg-type]
    assert rec.partial_fills == [
        {"client_order_id": cid, "requested": 19.0, "filled": 7.0, "status": "canceled"}
    ]
    assert rec.unfilled == [] and rec.budget_breaches == [] and len(gw.submits) == 1


def test_duplicate_is_adopted_not_resent(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    dry = executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=True)
    assert dry.items[0].order is not None
    cid = dry.items[0].order["client_order_id"]
    gw.orders[cid] = BrokerOrder(
        "b-x", cid, "SPY", "buy", 19.0, "accepted", 0.0, None, None, None, "limit", 526.26
    )
    assert outcomes(executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)) == ["submitted"]
    assert outcomes(executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)) == [
        "already_submitted"
    ]
    assert gw.submits == []


def test_insufficient_budget_and_insufficient_cash(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    small = replace(LOO, allocated_capital=400.0)  # effective 399 < one share at 500
    rep = executor(tmp_path, gw, now, cfg=small).sync(dry_run=False)
    assert outcomes(rep) == ["no_order_required"] and gw.submits == []
    gw.acct = replace(gw.acct, cash=9_000.0)  # worst case 19 x 526.26 = 9,998.94
    rep = executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)
    assert rep.items[0].detail.startswith("insufficient_buying_power") and gw.submits == []


def test_notional_cap_uses_the_limit_price(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    capped = replace(LOO, max_order_notional=9_990.0)  # 19 x 500 = 9,500 but 19 x 526.26 > cap
    rep = executor(tmp_path, FakeGateway(), now, cfg=capped).sync(dry_run=True)
    assert rep.items[0].detail.startswith("notional 9998.94 exceeds cap")


def test_sells_stay_market_on_open(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper", ["LONG"] * 5 + ["FLAT"])
    gw = FakeGateway()
    gw.positions["SPY"] = 19.0
    rep = executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)
    assert outcomes(rep) == ["submitted"]
    s = gw.submits[0]
    assert (s.side, s.qty, s.order_type, s.limit_price, s.time_in_force) == (
        "sell",
        19.0,
        "market",
        None,
        "opg",
    )


def test_rebalance_limit_bounds_the_whole_resulting_position(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    gw.positions["SPY"] = 5.0
    order = executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=True).items[0].order
    assert order is not None and (order["qty"], order["limit_price"]) == (14.0, 526.26)
    assert (5 + 14) * Decimal("526.26") <= Decimal(str(EFFECTIVE))


def test_opg_window_applies_to_limit_orders_and_is_rechecked(tmp_path: Path) -> None:
    account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    dry = executor(tmp_path, gw, et(2026, 10, 5, 17), cfg=LOO).sync(dry_run=True)
    assert dry.items[0].detail == "opg_window_not_open"
    ex = executor(tmp_path, gw, et(2026, 10, 6, 9, 26), cfg=LOO)
    times: Iterator[datetime] = iter([et(2026, 10, 6, 9, 26), et(2026, 10, 6, 9, 27, 30)])
    ex.clock = lambda: next(times)
    rep = ex.sync(dry_run=False)
    assert rep.items[0].detail == "opg_cutoff_passed" and gw.submits == []


def test_local_refusal_is_recorded_as_failed_without_sending(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")

    class Refuses(FakeGateway):
        def submit(self, r: OrderRequest) -> BrokerOrder:
            raise BrokerSafetyError("invalid limit_price: test")

    gw = Refuses()
    rep = executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)
    assert outcomes(rep) == ["failed"] and rep.items[0].detail.startswith("refused before")
    assert outcomes(executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)) == [
        "already_submitted"
    ]


# ── the real adapter over a simulated transport ──


def adapter(http: FakeHTTP) -> AlpacaPaperGateway:
    http.routes[("GET", "/v2/account")] = (
        200,
        {
            "account_number": "PA1",
            "status": "ACTIVE",
            "currency": "USD",
            "cash": "100000",
            "equity": "100000",
            "buying_power": "400000",
        },
    )
    return AlpacaPaperGateway(KEY, SECRET, transport=http)


def mutating(http: FakeHTTP) -> list[Any]:
    return [c for c in http.calls if c[0] != "GET"]


def test_end_to_end_single_limit_on_open_post(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    http = FakeHTTP()
    http.routes[("POST", "/v2/orders")] = (
        200,
        {**ORDER_JSON, "qty": "19", "type": "limit", "limit_price": "526.26"},
    )
    gw = adapter(http)
    rep = executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)  # type: ignore[arg-type]
    assert outcomes(rep) == ["submitted"]
    (post,) = mutating(http)
    body = post[3]
    assert {k: v for k, v in body.items() if k != "client_order_id"} == {
        "symbol": "SPY",
        "qty": "19",
        "side": "buy",
        "type": "limit",
        "time_in_force": "opg",
        "limit_price": "526.26",
    }
    assert Decimal(body["qty"]) * Decimal(body["limit_price"]) <= Decimal(str(EFFECTIVE))
    executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)  # type: ignore[arg-type]
    assert len(mutating(http)) == 1  # idempotent


def test_end_to_end_rejection_is_recorded_and_not_retried(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    http = FakeHTTP()
    http.routes[("POST", "/v2/orders")] = (422, {"message": "time_in_force opg not permitted"})
    gw = adapter(http)
    rep = executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)  # type: ignore[arg-type]
    assert outcomes(rep) == ["failed"] and "422" in rep.items[0].detail
    assert [a.event_type for a in rep.alerts] == ["BROKER_ORDER_FAILED"]
    executor(tmp_path, gw, now, cfg=LOO).sync(dry_run=False)  # type: ignore[arg-type]
    assert len(mutating(http)) == 1
