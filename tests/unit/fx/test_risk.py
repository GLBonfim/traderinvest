"""Central risk manager (ADR-0038): daily entries, persisted locks, kill switch, sizing, checks."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.fx.accounting import Book, Converter
from app.fx.calendar import FxCalendar
from app.fx.environment import Environment
from app.fx.models import DataOrigin, Direction, Side
from app.fx.orders import Position
from app.fx.risk import RiskConfig, RiskDecision, RiskManager, RiskState, TradeRequest
from tests.unit.fx.helpers import NOW, quote, quotes

CAL = FxCalendar()


def manager(**cfg: object) -> RiskManager:
    rm = RiskManager(RiskConfig(**cfg), calendar=CAL)  # type: ignore[arg-type]
    rm.observe(10_000.0, NOW)
    return rm


def long_req(
    stop: float = 1.0780,
    tp: float | None = 1.0840,
    score: float = 0.8,
    pid: str = "p1",
    sym: str = "EURUSD",
) -> TradeRequest:
    return TradeRequest(sym, Direction.LONG, stop, tp, score, pid)


def evaluate(
    rm: RiskManager,
    req: TradeRequest,
    q: dict | None = None,  # type: ignore[type-arg]
    positions: dict[str, Position] | None = None,
    now: datetime = NOW,
    equity: float = 10_000.0,
) -> RiskDecision:
    q = q or quotes()
    book = Book("EUR", equity, 30.0)
    for p in (positions or {}).values():
        book.positions[p.pair] = p
    return rm.evaluate(
        req,
        quotes=q,
        account=book.snapshot(q, now),
        positions=positions or {},
        now=now,
        currency="EUR",
    )


def codes(d: RiskDecision) -> set[str]:
    return {c.code for c in d.refusals}


def test_sizing_from_stop_distance_and_risk_per_trade() -> None:
    d = evaluate(manager(), long_req())
    assert d.approved and d.is_entry and d.side is Side.BUY
    q = quotes()["EURUSD"]
    dist = q.ask - 1.0780
    expected = (10_000 * 0.005) / (dist / 1.08) // 1000 * 1000  # 50 EUR at risk
    assert d.units == expected


def test_daily_entry_limit_reserve_consume_release() -> None:
    rm = manager(max_entries_per_day=2)
    rm.reserve("i1", "EURUSD")
    rm.reserve("i2", "GBPUSD")
    assert rm.entries_available == 0
    assert "entries_available" in codes(evaluate(rm, long_req()))
    rm.release("i2")  # rejected at the venue: the entry is given back
    assert rm.entries_available == 1
    rm.consume("i1")
    assert (rm.state.entries_used, rm.entries_available) == (1, 1)
    with pytest.raises(RuntimeError):
        rm.reserve("i3", "EURUSD")
        rm.reserve("i4", "EURUSD")


def test_exits_never_count_and_are_allowed_when_blocked() -> None:
    rm = manager(max_entries_per_day=0)
    rm.engage_kill_switch("test", NOW)
    pos = {"EURUSD": Position("EURUSD", 10_000, 1.08, NOW, "pos-1")}
    d = evaluate(rm, TradeRequest("EURUSD", Direction.FLAT, None, None, 1.0, "x"), positions=pos)
    assert d.approved and not d.is_entry and d.side is Side.SELL and d.units == 10_000
    nothing = evaluate(rm, TradeRequest("GBPUSD", Direction.FLAT, None, None, 1.0, "y"))
    assert not nothing.approved


def test_daily_loss_lock_persists_until_next_trading_day() -> None:
    rm = manager()
    assert rm.observe(9_600.0, NOW + timedelta(hours=1)) == []
    assert rm.observe(9_499.0, NOW + timedelta(hours=2)) == ["lock:daily_loss"]  # -5.01 %
    assert rm.should_flatten()
    assert "daily_lock" in codes(evaluate(rm, long_req()))
    # a recovery the same day does not release the lock
    rm.observe(10_200.0, NOW + timedelta(hours=3))
    assert rm.state.lock == "daily_loss"
    # restart: the lock comes back from the persisted state
    again = RiskManager(rm.config, RiskState.from_dict(rm.state.to_dict()), CAL)
    again.observe(10_200.0, NOW + timedelta(hours=4))
    assert again.state.lock == "daily_loss"
    # next trading day (after 17:00 New York): released, entries reset, new day-start equity
    events = again.observe(10_150.0, datetime(2026, 10, 14, 21, 1, tzinfo=UTC))
    assert events == ["day_rolled"]
    assert again.state.lock is None and again.state.day_start_equity == 10_150.0


def test_profit_target_lock_and_flatten_policy() -> None:
    rm = manager()
    assert rm.observe(11_500.0, NOW + timedelta(hours=1)) == ["lock:profit_target"]
    assert not rm.should_flatten()  # default: keep positions, block new entries
    assert "daily_lock" in codes(evaluate(rm, long_req(), equity=11_500.0))
    rm2 = manager(flatten_on_profit_target=True)
    rm2.observe(11_600.0, NOW)
    assert rm2.should_flatten()


def test_kill_switch_blocks_entries_and_needs_the_confirmation_word() -> None:
    rm = manager()
    rm.engage_kill_switch("manual", NOW)
    assert "kill_switch" in codes(evaluate(rm, long_req()))
    with pytest.raises(ValueError):
        rm.release_kill_switch("sim")
    # the kill switch survives a day roll and a restart
    again = RiskManager(rm.config, RiskState.from_dict(rm.state.to_dict()), CAL)
    again.observe(10_000.0, NOW + timedelta(days=1))
    assert again.state.kill_switch
    again.release_kill_switch("LIBERTAR")
    assert evaluate(
        again, long_req(pid="p2"), now=NOW + timedelta(days=1), q=quotes(ts=NOW + timedelta(days=1))
    ).approved


@pytest.mark.parametrize(
    ("req", "q", "now", "code"),
    [
        (long_req(stop=1.0810), None, NOW, "stop_loss"),  # stop above a long entry
        (long_req(stop=None), None, NOW, "stop_loss"),  # type: ignore[arg-type]
        (long_req(score=0.3), None, NOW, "score"),
        (long_req(tp=1.0805), None, NOW, "risk_reward"),
        (long_req(), {"EURUSD": quote("EURUSD", 1.08, spread_pips=6)}, NOW, "spread"),
        (long_req(), quotes(ts=NOW - timedelta(minutes=10)), NOW, "quote_fresh"),
        (
            long_req(),
            quotes(ts=datetime(2026, 10, 14, 20, 50, tzinfo=UTC)),
            datetime(2026, 10, 14, 20, 50, tzinfo=UTC),
            "rollover_window",
        ),
        (
            long_req(),
            quotes(ts=datetime(2026, 10, 17, 12, tzinfo=UTC)),
            datetime(2026, 10, 17, 12, tzinfo=UTC),
            "market_open",
        ),
    ],
)
def test_entry_checks_refuse(
    req: TradeRequest,
    q: dict | None,
    now: datetime,  # type: ignore[type-arg]
    code: str,
) -> None:
    d = evaluate(manager(), req, q=q, now=now)
    assert not d.approved and code in codes(d) and d.units == 0


def test_demo_refuses_non_live_prices_simulation_accepts_synthetic() -> None:
    rm = manager()
    assert evaluate(rm, long_req()).approved  # SIMULATION trades synthetic prices
    demo = RiskManager(rm.config, replace(rm.state), CAL, Environment.DEMO)
    assert "data_origin" in codes(evaluate(demo, long_req()))
    live = quotes()
    live = {k: replace(v, origin=DataOrigin.LIVE) for k, v in live.items()}
    assert evaluate(demo, long_req(), q=live).approved
    real = RiskManager(rm.config, replace(rm.state), CAL, Environment.REAL)
    assert "environment" in codes(evaluate(real, long_req()))


def test_leverage_cap_reduces_size_and_positions_limit() -> None:
    rm = manager(risk_per_trade_pct=5.0, daily_loss_limit_pct=10.0, max_leverage=2.0)
    d = evaluate(rm, long_req(stop=1.0795))
    assert d.approved and d.units == 20_000  # 2x of 10 000 EUR, EUR base
    assert any(c.code == "leverage_cap" for c in d.checks)
    rm2 = manager(max_open_positions=1)
    pos = {"GBPUSD": Position("GBPUSD", 5_000, 1.27, NOW, "pos-1")}
    assert "max_positions" in codes(evaluate(rm2, long_req(), positions=pos))


def test_no_pyramiding_and_reversal_counts_as_entry_including_the_close() -> None:
    rm = manager()
    pos = {"EURUSD": Position("EURUSD", 10_000, 1.08, NOW, "pos-1")}
    assert "no_pyramiding" in codes(evaluate(rm, long_req(), positions=pos))
    short = {"EURUSD": Position("EURUSD", -10_000, 1.08, NOW, "pos-2")}
    d = evaluate(rm, long_req(), positions=short)
    assert d.approved and d.is_entry
    fresh = evaluate(rm, long_req(pid="p9"))
    assert d.units == fresh.units + 10_000


def test_currency_exposure_limit() -> None:
    rm = manager(max_currency_exposure_pct=100.0)
    pos = {"EURGBP": Position("EURGBP", 9_000, 0.85, NOW, "pos-1")}
    d = evaluate(rm, long_req(), positions=pos)
    assert "currency_exposure" in codes(d)


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        RiskConfig(risk_per_trade_pct=6, daily_loss_limit_pct=5)
    with pytest.raises(ValueError):
        RiskConfig(max_entries_per_day=-1)
    with pytest.raises(ValueError):
        RiskState.from_dict({"version": 99})


def test_conversion_used_for_sizing_jpy_pair() -> None:
    q = quotes()
    req = TradeRequest("USDJPY", Direction.SHORT, 150.40, 149.20, 0.8, "j1")
    d = evaluate(manager(), req, q=q)
    assert d.approved and d.side is Side.SELL
    dist = 150.40 - q["USDJPY"].bid
    expected = (50.0 / (dist * Converter(q).rate("JPY", "EUR"))) // 1000 * 1000
    assert d.units == expected
