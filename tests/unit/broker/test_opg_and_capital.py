"""ADR-0026 protections, without any network and without the real clock: the OPG submission
window (cut-off, queue opening, daily rejection band, weekends, holidays, DST), the sandbox
capital policy (market buys are unbounded, so blocked by default), the sandbox account check
(tradable, cash without margin), duplicates, partial fills and rejections — through fake
gateways and through the real adapter over a simulated transport."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca import AlpacaPaperGateway
from app.broker.config import CAPITAL_BLOCK_UNBOUNDED, OPG_WHOLE_SHARES, BrokerConfig
from app.broker.executor import Intent
from app.broker.gateway import BrokerOrder, BrokerRequestError, OrderRequest
from app.broker.reconcile import reconcile
from app.broker.timing import NEW_YORK, opg_block_reason, opg_window
from tests.unit.broker.test_broker import (
    CAL,
    KEY,
    MECH,
    ORDER_JSON,
    SECRET,
    FakeGateway,
    FakeHTTP,
    account_with_pending_entry,
    executor,
    outcomes,
)


def et(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0, us: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, us, tzinfo=NEW_YORK)


def utc(y: int, mo: int, d: int, h: int, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


OK, NOT_OPEN, CUTOFF, BAND = "", "opg_window_not_open", "opg_cutoff_passed", "opg_rejection_window"
MON_OPEN = et(2026, 10, 12, 9, 30)  # Monday 2026-10-12; previous session Friday 2026-10-09
TUE_OPEN_AFTER_LABOR_DAY = et(2026, 9, 8, 9, 30)  # Labor Day Mon 2026-09-07
FRI_OPEN_AFTER_THANKSGIVING = et(2026, 11, 27, 9, 30)  # Thanksgiving Thu 2026-11-26
OPEN_AFTER_DST_END = utc(2026, 11, 2, 14, 30)  # DST ends Sun 2026-11-01 (09:30 EST)
OPEN_AFTER_DST_START = utc(2026, 3, 9, 13, 30)  # DST starts Sun 2026-03-08 (09:30 EDT)

MARKET = BrokerConfig(order_policy=OPG_WHOLE_SHARES)  # market-on-open buys: unbounded cost

# The executor fixture: decision at the close of Mon 2026-10-05, pending for Tue 2026-10-06.
FIXTURE_IN_WINDOW = et(2026, 10, 5, 20)


# ── OPG window: pure function ──


@pytest.mark.parametrize(
    ("now", "safety", "expected"),
    [
        (et(2026, 10, 8, 20), 60, NOT_OPEN),  # Thursday night: would reach Friday's auction
        (et(2026, 10, 9, 10), 60, NOT_OPEN),  # during the previous session
        (et(2026, 10, 9, 18, 59), 60, NOT_OPEN),
        (et(2026, 10, 9, 19, 0, 30), 60, NOT_OPEN),  # inside the safety margin
        (et(2026, 10, 9, 19, 1), 60, OK),  # exactly at the (inclusive) opening
        (et(2026, 10, 9, 18, 59, 59), 0, NOT_OPEN),
        (et(2026, 10, 9, 19, 0), 0, OK),
        (et(2026, 10, 10, 8), 60, OK),  # Saturday morning
        (et(2026, 10, 10, 12), 60, BAND),  # Saturday midday: the band applies every day
        (et(2026, 10, 10, 20), 60, OK),
        (et(2026, 10, 11, 23), 60, OK),  # Sunday night
        (et(2026, 10, 12, 9, 26, 59), 60, OK),
        (et(2026, 10, 12, 9, 27), 60, CUTOFF),  # exactly at the (exclusive) cut-off
        (et(2026, 10, 12, 9, 27, 59, 999_999), 0, OK),
        (et(2026, 10, 12, 9, 28), 0, CUTOFF),  # Alpaca's own limit
        (et(2026, 10, 12, 9, 29), 60, CUTOFF),
        (et(2026, 10, 12, 12), 60, CUTOFF),
    ],
)
def test_opg_window_monday_auction(now: datetime, safety: float, expected: str) -> None:
    assert opg_block_reason(CAL, MON_OPEN, now, safety) == expected


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (et(2026, 10, 10, 9, 27, 59), OK),
        (et(2026, 10, 10, 9, 28), BAND),
        (et(2026, 10, 10, 18, 59, 59), BAND),
        (et(2026, 10, 10, 19), OK),
    ],
)
def test_daily_rejection_band_boundaries(now: datetime, expected: str) -> None:
    assert opg_block_reason(CAL, MON_OPEN, now, 0) == expected


@pytest.mark.parametrize(
    ("scheduled_open", "now", "expected"),
    [
        (TUE_OPEN_AFTER_LABOR_DAY, et(2026, 9, 4, 12), NOT_OPEN),  # previous session: Friday
        (TUE_OPEN_AFTER_LABOR_DAY, et(2026, 9, 4, 20), OK),
        (TUE_OPEN_AFTER_LABOR_DAY, et(2026, 9, 7, 8), OK),  # the holiday itself
        (TUE_OPEN_AFTER_LABOR_DAY, et(2026, 9, 7, 12), BAND),
        (TUE_OPEN_AFTER_LABOR_DAY, et(2026, 9, 7, 20), OK),
        (TUE_OPEN_AFTER_LABOR_DAY, et(2026, 9, 8, 9, 27, 30), OK),
        (FRI_OPEN_AFTER_THANKSGIVING, et(2026, 11, 25, 18), NOT_OPEN),  # previous: Wednesday
        (FRI_OPEN_AFTER_THANKSGIVING, et(2026, 11, 26, 20), OK),
        (FRI_OPEN_AFTER_THANKSGIVING, et(2026, 11, 27, 9, 28), CUTOFF),  # early-close day
    ],
)
def test_opg_window_around_holidays(scheduled_open: datetime, now: datetime, expected: str) -> None:
    assert opg_block_reason(CAL, scheduled_open, now, 0) == expected


def test_holiday_window_uses_the_previous_session_not_yesterday() -> None:
    w = opg_window(CAL, TUE_OPEN_AFTER_LABOR_DAY, 0)
    assert (w.session.isoformat(), w.previous_session.isoformat()) == ("2026-09-08", "2026-09-04")
    assert opg_window(CAL, FRI_OPEN_AFTER_THANKSGIVING, 0).previous_session.isoformat() == (
        "2026-11-25"
    )


@pytest.mark.parametrize(
    ("scheduled_open", "now", "expected"),
    [
        # Friday 19:01 is EDT (23:01Z); Monday 09:27 is EST (14:27Z)
        (OPEN_AFTER_DST_END, utc(2026, 10, 30, 23, 0, 59), NOT_OPEN),
        (OPEN_AFTER_DST_END, utc(2026, 10, 30, 23, 1), OK),
        (OPEN_AFTER_DST_END, utc(2026, 11, 2, 1, 0), OK),  # Sunday 20:00 EST
        (OPEN_AFTER_DST_END, utc(2026, 11, 2, 13, 27), OK),  # 08:27 EST (an EDT bug would cut)
        (OPEN_AFTER_DST_END, utc(2026, 11, 2, 14, 26, 59), OK),
        (OPEN_AFTER_DST_END, utc(2026, 11, 2, 14, 27), CUTOFF),
        # Friday 19:01 is EST (00:01Z Saturday); Monday 09:27 is EDT (13:27Z)
        (OPEN_AFTER_DST_START, utc(2026, 3, 7, 0, 0, 59), NOT_OPEN),
        (OPEN_AFTER_DST_START, utc(2026, 3, 7, 0, 1), OK),
        (OPEN_AFTER_DST_START, utc(2026, 3, 9, 13, 26, 59), OK),
        (OPEN_AFTER_DST_START, utc(2026, 3, 9, 13, 27), CUTOFF),
    ],
)
def test_opg_window_across_dst(scheduled_open: datetime, now: datetime, expected: str) -> None:
    assert opg_block_reason(CAL, scheduled_open, now, 60) == expected


def test_invalid_schedules_and_naive_times() -> None:
    sat = et(2026, 10, 10, 9, 30)  # Saturday is not a session
    assert opg_block_reason(CAL, sat, et(2026, 10, 9, 20), 0).startswith("opg_schedule_invalid")
    not_open = et(2026, 10, 12, 10)  # a session day, but not its open
    assert opg_block_reason(CAL, not_open, et(2026, 10, 9, 20), 0).startswith(
        "opg_schedule_invalid"
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        opg_block_reason(CAL, MON_OPEN, datetime(2026, 10, 10, 20), 0)


# ── executor: OPG window at the submission point ──


def test_fixture_time_is_inside_the_window(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    assert now == FIXTURE_IN_WINDOW


def test_executor_blocks_outside_the_window_and_dry_run_records_nothing(tmp_path: Path) -> None:
    account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    for now, reason in [
        (et(2026, 10, 5, 17), NOT_OPEN),  # after the decision, before the 19:00 queue opening
        (et(2026, 10, 6, 9, 28), CUTOFF),  # instruction not stale yet, but too late for opg
    ]:
        dry = executor(tmp_path, gw, now).sync(dry_run=True)
        assert outcomes(dry) == ["blocked"] and dry.items[0].detail == reason
        assert dry.alerts == []
        rep = executor(tmp_path, gw, now).sync(dry_run=False)
        assert rep.items[0].detail == reason and gw.submits == []
        assert [a.event_type for a in rep.alerts] == ["BROKER_ORDER_BLOCKED"]
    # a blocked attempt does not prevent a later, valid one
    later = executor(tmp_path, gw, FIXTURE_IN_WINDOW).sync(dry_run=False)
    assert outcomes(later) == ["submitted"] and len(gw.submits) == 1


def test_window_is_rechecked_immediately_before_the_post(tmp_path: Path) -> None:
    account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    ex = executor(tmp_path, gw, et(2026, 10, 6, 9, 26))
    times: Iterator[datetime] = iter([et(2026, 10, 6, 9, 26), et(2026, 10, 6, 9, 27, 30)])
    ex.clock = lambda: next(times)  # gates pass at 09:26; the POST point is reached at 09:27:30
    rep = ex.sync(dry_run=False)
    assert outcomes(rep) == ["blocked"] and rep.items[0].detail == CUTOFF
    assert gw.submits == []
    assert rep.items[0].intent is not None
    assert ex.store.orders()[rep.items[0].intent["client_order_id"]]["status"] == "blocked"


def test_day_policy_is_not_subject_to_the_opg_window(tmp_path: Path) -> None:
    account_with_pending_entry(tmp_path / "paper")
    day = replace(MECH, order_policy="day_fractional")
    rep = executor(tmp_path, FakeGateway(), et(2026, 10, 5, 17), cfg=day).sync(dry_run=True)
    assert outcomes(rep) == ["would_submit"] and rep.items[0].order is not None
    assert rep.items[0].order["time_in_force"] == "day"


# ── capital policy, budget and the sandbox account ──


def test_default_capital_policy_blocks_every_market_buy(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    assert BrokerConfig().capital_policy == CAPITAL_BLOCK_UNBOUNDED
    assert MARKET.capital_policy == CAPITAL_BLOCK_UNBOUNDED
    dry = executor(tmp_path, gw, now, cfg=MARKET).sync(dry_run=True)
    assert dry.items[0].detail.startswith("capital_bound_unenforceable")
    rep = executor(tmp_path, gw, now, cfg=MARKET).sync(dry_run=False)
    assert outcomes(rep) == ["blocked"] and gw.submits == []
    assert rep.items[0].detail.startswith("capital_bound_unenforceable")
    assert [a.event_type for a in rep.alerts] == ["BROKER_ORDER_BLOCKED"]


def test_capital_checks_never_hold_back_an_exit(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper", ["LONG"] * 5 + ["FLAT"])
    gw = FakeGateway()
    gw.positions["SPY"] = 13.0
    rep = executor(tmp_path, gw, now, cfg=BrokerConfig()).sync(dry_run=False)
    assert outcomes(rep) == ["submitted"]
    assert (gw.submits[0].side, gw.submits[0].qty) == ("sell", 13.0)


def test_unguaranteed_policy_checks_the_budget_at_the_reference_price(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    gw.positions["SPY"] = 5.0  # rebalance: 5 held + 15 bought = 20 x 500 = 10,000 <= budget
    assert outcomes(executor(tmp_path, gw, now).sync(dry_run=True)) == ["would_submit"]
    # Floor sizing never exceeds the budget at the reference price, so the check is defensive:
    # exercised directly with a quantity beyond the sizing.
    ex = executor(tmp_path, gw, now)
    rep = ex.sync(dry_run=True)
    assert rep.items[0].intent is not None
    intent = Intent(**rep.items[0].intent)
    assert ex._capital_block(intent, held=5.0, qty=15.0) == ""  # 20 x 500 = 10,000
    assert ex._capital_block(intent, held=5.0, qty=16.0).startswith("sandbox_budget_exceeded")


def test_open_price_above_budget_is_detected_by_reconciliation(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    ex = executor(tmp_path, gw, now)
    assert outcomes(ex.sync(dry_run=False)) == ["submitted"]  # 20 shares sized at 500
    cid = gw.submits[0].client_order_id
    gw.orders[cid] = replace(
        gw.orders[cid], status="filled", filled_qty=20.0, filled_avg_price=520.0
    )
    refreshed = executor(tmp_path, gw, now)
    refreshed.sync(dry_run=False)  # restart: the refresh records the fill
    rec = reconcile(MECH, refreshed.store, gw, tmp_path / "paper", now)  # type: ignore[arg-type]
    assert len(rec.budget_breaches) == 1 and rec.budget_breaches[0]["filled_cost"] == 10_400.0
    assert "BROKER_RECONCILIATION_MISMATCH" in [a.event_type for a in rec.alerts]


def test_insufficient_buying_power_and_untradable_account(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    gw.acct = replace(gw.acct, cash=5_000.0)  # 20 x 500 = 10,000 needed, no margin
    rep = executor(tmp_path, gw, now).sync(dry_run=False)
    assert rep.items[0].detail.startswith("insufficient_buying_power") and gw.submits == []
    gw.acct = replace(gw.acct, cash=100_000.0, buying_power=4_000.0)  # e.g. reduced by open orders
    rep = executor(tmp_path, gw, now).sync(dry_run=True)
    assert rep.items[0].detail.startswith("insufficient_buying_power")
    gw.acct = replace(gw.acct, buying_power=400_000.0, trading_blocked=True)
    rep = executor(tmp_path, gw, now).sync(dry_run=True)
    assert rep.items[0].detail.startswith("sandbox_account_not_tradable")
    gw.acct = replace(gw.acct, trading_blocked=False, status="ACCOUNT_UPDATED")
    assert (
        executor(tmp_path, gw, now)
        .sync(dry_run=True)
        .items[0]
        .detail.startswith("sandbox_account_not_tradable")
    )


def test_account_lookup_failure_blocks(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")

    class Down(FakeGateway):
        def account(self) -> Any:
            raise BrokerRequestError("account: HTTP 503", status=503)

    gw = Down()
    rep = executor(tmp_path, gw, now).sync(dry_run=False)
    assert rep.items[0].detail.startswith("sandbox_account_unavailable") and gw.submits == []


# ── duplicates, partial fills, rejections ──


def test_order_already_at_the_broker_is_adopted_not_duplicated(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    dry = executor(tmp_path, gw, now).sync(dry_run=True)
    assert dry.items[0].order is not None
    cid = dry.items[0].order["client_order_id"]
    gw.orders[cid] = BrokerOrder("b-x", cid, "SPY", "buy", 20.0, "accepted", 0.0, None, None, None)
    rep = executor(tmp_path, gw, now).sync(dry_run=False)
    assert outcomes(rep) == ["submitted"] and gw.submits == []  # lookup before POST adopts it
    for _ in range(2):
        assert outcomes(executor(tmp_path, gw, now).sync(dry_run=False)) == ["already_submitted"]
    assert gw.submits == []


def test_partial_opg_fill_is_reported(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    gw = FakeGateway()
    ex = executor(tmp_path, gw, now)
    ex.sync(dry_run=False)
    cid = gw.submits[0].client_order_id
    # auction filled 5 of 20; Alpaca cancels the rest of an opg order after the open
    gw.orders[cid] = replace(
        gw.orders[cid], status="canceled", filled_qty=5.0, filled_avg_price=501.0
    )
    refreshed = executor(tmp_path, gw, now)
    rep = refreshed.sync(dry_run=False)
    assert rep.reconciled == [{"client_order_id": cid, "status": "canceled", "filled_qty": 5.0}]
    rec = reconcile(MECH, refreshed.store, gw, tmp_path / "paper", now)  # type: ignore[arg-type]
    assert rec.partial_fills == [
        {"client_order_id": cid, "requested": 20.0, "filled": 5.0, "status": "canceled"}
    ]
    assert rec.budget_breaches == [] and len(gw.submits) == 1  # nothing corrected automatically


def test_broker_rejection_is_recorded_and_never_blindly_retried(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")

    class Rejects(FakeGateway):
        def submit(self, r: OrderRequest) -> BrokerOrder:
            self.submits.append(r)
            raise BrokerRequestError("order submission: HTTP 422 opg order rejected", status=422)

    gw = Rejects()
    rep = executor(tmp_path, gw, now).sync(dry_run=False)
    assert outcomes(rep) == ["failed"] and "422" in rep.items[0].detail
    assert [a.event_type for a in rep.alerts] == ["BROKER_ORDER_FAILED"]
    again = executor(tmp_path, gw, now).sync(dry_run=False)
    assert outcomes(again) == ["already_submitted"] and len(gw.submits) == 1


# ── the real adapter over a simulated transport (no network) ──


ACCOUNT_JSON = {
    "account_number": "PA1",
    "status": "ACTIVE",
    "currency": "USD",
    "cash": "100000",
    "equity": "100000",
    "buying_power": "400000",
    "trading_blocked": False,
    "account_blocked": False,
}


def adapter(http: FakeHTTP) -> AlpacaPaperGateway:
    http.routes[("GET", "/v2/account")] = (200, ACCOUNT_JSON)
    return AlpacaPaperGateway(KEY, SECRET, transport=http)


def posts(http: FakeHTTP) -> list[Any]:
    return [c for c in http.calls if c[0] != "GET"]


def test_adapter_end_to_end_market_buy_sends_nothing(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    http = FakeHTTP()
    rep = executor(tmp_path, adapter(http), now, cfg=MARKET).sync(dry_run=False)  # type: ignore[arg-type]
    assert rep.items[0].detail.startswith("capital_bound_unenforceable")
    assert posts(http) == []


def test_adapter_end_to_end_single_post_with_exact_payload(tmp_path: Path) -> None:
    acct, now = account_with_pending_entry(tmp_path / "paper")
    http = FakeHTTP()
    gw = adapter(http)
    cid_prefix = f"qp-{acct}-D"
    http.routes[("POST", "/v2/orders")] = (200, {**ORDER_JSON, "qty": "20"})
    rep = executor(tmp_path, gw, now).sync(dry_run=False)  # type: ignore[arg-type]
    assert outcomes(rep) == ["submitted"]
    (post,) = posts(http)
    assert post[0] == "POST" and post[3]["client_order_id"].startswith(cid_prefix)
    assert {k: v for k, v in post[3].items() if k != "client_order_id"} == {
        "symbol": "SPY",
        "qty": "20",
        "side": "buy",
        "type": "market",
        "time_in_force": "opg",
    }
    # the GET lookup before the POST, then the POST; nothing else mutating
    assert [c[0] for c in http.calls].count("POST") == 1


def test_adapter_end_to_end_rejection_is_not_retried(tmp_path: Path) -> None:
    _, now = account_with_pending_entry(tmp_path / "paper")
    http = FakeHTTP()
    gw = adapter(http)
    http.routes[("POST", "/v2/orders")] = (422, {"message": "insufficient buying power"})
    rep = executor(tmp_path, gw, now).sync(dry_run=False)  # type: ignore[arg-type]
    assert outcomes(rep) == ["failed"] and "422" in rep.items[0].detail
    assert SECRET not in rep.items[0].detail and KEY not in rep.items[0].detail
    executor(tmp_path, gw, now).sync(dry_run=False)  # type: ignore[arg-type]
    assert len(posts(http)) == 1
