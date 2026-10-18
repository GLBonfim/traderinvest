"""End-to-end orchestration on the simulator: lifecycle, limits, ambiguity, recovery."""

from collections import Counter
from pathlib import Path

from app.fx.lifecycle import OrderState
from app.fx.models import Direction
from app.fx.orchestrator import Orchestrator
from app.fx.risk import TradeRequest
from app.fx.store import FxStore
from tests.unit.fx.helpers import orchestrator, small_config


def long_request(o: Orchestrator, sym: str = "EURUSD", pid: str = "t-1") -> TradeRequest:
    q = o.market.quotes[sym]
    pip = 0.01 if sym.endswith("JPY") else 0.0001
    return TradeRequest(
        sym, Direction.LONG, round(q.mid - 30 * pip, 5), round(q.mid + 60 * pip, 5), 0.9, pid
    )


def states(it: object) -> list[str]:
    return [h[0] for h in it.history]  # type: ignore[attr-defined]


def test_every_fill_went_through_the_whole_lifecycle(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    it = o.process(long_request(o), "trend")
    assert it is not None and it.state is OrderState.FILLED
    assert states(it) == ["signal", "proposal", "authorized", "sent", "accepted", "filled"]
    assert o.risk.state.entries_used == 1 and not o.risk.state.reservations
    assert o.owners["EURUSD"] == "trend"
    again = o.process(long_request(o), "trend")  # same proposal id: no second order
    assert again is it and len(o.broker.orders) == 1
    kinds = [e["kind"] for e in o.store.recent(50)]
    assert kinds.index("risk_decision") < kinds.index("order_sent") < kinds.index("order_result")
    assert o.store.verify() == (True, None)


def test_refused_requests_never_reach_the_broker(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    o.engage_kill_switch("test")
    it = o.process(long_request(o), "trend")
    assert it is not None and it.state is OrderState.REFUSED and "kill switch" in it.reason
    assert o.broker.orders == {} and o.risk.entries_available == 5


def test_daily_entry_limit_is_enforced(tmp_path: Path) -> None:
    cfg = small_config()
    cfg = cfg.model_copy(
        update={
            "risk": cfg.risk.model_copy(update={"max_entries_per_day": 2, "max_open_positions": 4})
        }
    )
    o = orchestrator(tmp_path, cfg)
    results = [
        o.process(long_request(o, s, f"p-{s}"), "trend") for s in ("EURUSD", "GBPUSD", "USDJPY")
    ]
    assert [r.state for r in results if r] == [
        OrderState.FILLED,
        OrderState.FILLED,
        OrderState.REFUSED,
    ]
    # exits are always possible and do not count
    close = o.process(TradeRequest("EURUSD", Direction.FLAT, None, None, 1.0, "c-1"), None)
    assert close is not None and close.state is OrderState.FILLED and not close.is_entry
    assert o.risk.state.entries_used == 2


def test_rejection_releases_the_reserved_entry(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    o.broker.faults.reject_next = 1
    it = o.process(long_request(o), "trend")
    assert it is not None and it.state is OrderState.REJECTED
    assert o.risk.state.entries_used == 0 and o.risk.entries_available == 5


def test_ambiguous_timeout_is_resolved_by_lookup_not_resend(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    o.broker.faults.timeout_next = 1
    it = o.process(long_request(o), "trend")
    assert it is not None and it.state is OrderState.FILLED
    assert "unknown" in states(it)
    assert len(o.broker.orders) == 1 and len(o.broker.positions()) == 1
    assert o.risk.state.entries_used == 1


def test_partial_fill_consumes_the_entry(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    o.broker.faults.partial_next = 1
    it = o.process(long_request(o), "trend")
    assert it is not None and it.state is OrderState.CANCELLED  # remainder cancelled (M6)
    assert "partially_filled" in states(it) and 0 < it.filled_units < it.units
    assert o.risk.state.entries_used == 1


def test_recovery_after_restart_keeps_positions_limits_and_locks(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    o.process(long_request(o), "trend")
    o.engage_kill_switch("teste")
    for _ in range(40):
        o.tick()
    before = (o.broker.book.to_dict(), o.risk.state.to_dict(), o.market.now)
    o.save()  # graceful shutdown (the engine saves on stop)
    o.store.close()
    r = Orchestrator(small_config(), FxStore(tmp_path / "fx.db"))
    assert r.recovered
    assert r.broker.book.to_dict() == before[0]
    assert r.risk.state.kill_switch and r.risk.state.entries_used == before[1]["entries_used"]
    assert r.market.now == before[2]
    assert [e["kind"] for e in r.store.recent(1)] == ["engine_recovered"]


def test_crash_without_save_restores_the_last_consistent_snapshot(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    o.process(long_request(o), "trend")  # order events save immediately
    saved = (o.broker.book.to_dict(), o.risk.state.to_dict())
    for _ in range(5):  # fewer than the periodic save interval, no order events
        o.tick()
    o.store.close()  # crash: no final save
    r = Orchestrator(small_config(), FxStore(tmp_path / "fx.db"))
    assert r.broker.book.to_dict() == saved[0]
    assert r.risk.state.entries_used == saved[1]["entries_used"]


def test_intent_left_sent_before_a_crash_is_resolved_at_the_venue(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    it = o.process(long_request(o), "trend")
    assert it is not None
    it.state = OrderState.SENT  # simulate a crash after the venue filled, before the reply
    o.save()
    o.store.close()
    r = Orchestrator(small_config(), FxStore(tmp_path / "fx.db"))
    rec = r.intents[it.intent_id]
    assert rec.state is OrderState.FILLED and "(recuperado)" in rec.history[-1][2]


def test_daily_loss_lock_flattens_positions(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    o.process(long_request(o), "trend")
    assert o.broker.positions()
    o.risk.state.day_start_equity = o.broker.account().equity / 0.94  # pretend -6 % today
    o.tick()
    assert o.risk.state.lock == "daily_loss"
    assert o.broker.positions() == []
    kinds = Counter(e["kind"] for e in o.store.recent(100))
    assert kinds["risk_lock"] == 1 and kinds["flatten_all"] == 1


def test_simulation_runs_and_respects_limits(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    days: Counter[str] = Counter()
    for _ in range(900):
        o.tick()
    for it in o.intents.values():
        if it.is_entry and it.filled_units > 0:
            days[
                o.calendar.trading_day(
                    __import__("datetime").datetime.fromisoformat(it.created_at)
                ).isoformat()
            ] += 1
        if it.filled_units > 0:
            assert {"authorized", "sent"} <= set(states(it))
    assert all(n <= 5 for n in days.values())
    snap = o.snapshot()
    assert snap["environment"] == "simulation" and snap["data_origin"] == "synthetic"
    assert set(snap) >= {"account", "risk", "quotes", "positions", "agents", "events", "charts"}
    assert o.store.verify() == (True, None)
