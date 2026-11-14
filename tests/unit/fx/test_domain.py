"""FX domain basics: pairs, quotes, environments (REAL blocked), calendar, lifecycle, config."""

from datetime import UTC, date, datetime

import pytest

from app.core.safety import RealMoneyExecutionBlockedError
from app.fx.calendar import FxCalendar
from app.fx.config import FxConfig, RiskSettings, load_config, merge, save_config
from app.fx.environment import Environment, parse_environment, require_executable
from app.fx.lifecycle import IllegalTransitionError, OrderState, can_transition, transition
from app.fx.models import Direction, Pair, Quote, Side, pair
from tests.unit.fx.helpers import NOW, quote

CAL = FxCalendar()


def utc(*a: int) -> datetime:
    return datetime(*a, tzinfo=UTC)  # type: ignore[misc]


# --- models ------------------------------------------------------------------------------


def test_pairs_and_pips() -> None:
    assert pair("eur/usd").symbol == "EURUSD"
    assert pair("USDJPY").pip_size == 0.01
    assert pair("EURUSD").pips(0.0012) == pytest.approx(12)
    with pytest.raises(KeyError):
        pair("XAUUSD")
    for bad in (("EU", "USD"), ("USD", "USD"), ("eur", "usd")):
        with pytest.raises(ValueError):
            Pair(bad[0], bad[1], 0.0001)


def test_quote_sides_and_marks() -> None:
    q = quote("EURUSD", 1.08, spread_pips=2.0)
    assert q.price_for(Side.BUY) == q.ask > q.mid > q.bid == q.price_for(Side.SELL)
    assert q.mark_for(1000) == q.bid  # a long closes at the bid
    assert q.mark_for(-1000) == q.ask  # a short closes at the ask
    assert q.spread == pytest.approx(0.0002)


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValueError, match="timezone"):
        Quote("EURUSD", 1.0, 1.1, datetime(2026, 1, 1), quote("EURUSD", 1).origin, "t")


def test_direction_entry_side() -> None:
    assert Direction.LONG.entry_side is Side.BUY
    assert Direction.SHORT.entry_side is Side.SELL
    with pytest.raises(ValueError):
        _ = Direction.FLAT.entry_side


# --- environments ------------------------------------------------------------------------


def test_real_environment_is_always_refused() -> None:
    assert require_executable(Environment.SIMULATION) is Environment.SIMULATION
    assert require_executable(Environment.DEMO) is Environment.DEMO
    with pytest.raises(RealMoneyExecutionBlockedError):
        require_executable(Environment.REAL)
    assert parse_environment(" Demo ") is Environment.DEMO
    with pytest.raises(ValueError):
        parse_environment("live")


def test_config_refuses_real_and_validates(tmp_path: object) -> None:
    with pytest.raises(ValueError, match="REAL"):
        FxConfig(environment=Environment.REAL)
    with pytest.raises(ValueError):
        FxConfig(account_currency="BTC")
    with pytest.raises(ValueError):
        FxConfig(pairs=["XAUUSD"])
    with pytest.raises(ValueError):
        RiskSettings(risk_per_trade_pct=3, daily_loss_limit_pct=2)
    with pytest.raises(ValueError):
        RiskSettings(max_leverage=50)  # above the ESMA retail cap


def test_config_roundtrip_and_merge(tmp_path: object) -> None:
    from pathlib import Path

    root = Path(str(tmp_path))
    assert load_config(root) == FxConfig()  # defaults when no file
    cfg = merge(FxConfig(), {"risk": {"max_entries_per_day": 3}, "account_currency": "usd"})
    assert cfg.risk.max_entries_per_day == 3 and cfg.risk.daily_loss_limit_pct == 5.0
    assert cfg.account_currency == "USD"
    save_config(cfg, root)
    assert load_config(root) == cfg
    rc = cfg.risk.to_risk_config()
    assert (rc.max_entries_per_day, rc.daily_loss_limit_pct, rc.daily_profit_target_pct) == (
        3,
        5.0,
        15.0,
    )


# --- calendar ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ts", "day"),
    [
        (utc(2026, 10, 14, 20, 59), date(2026, 10, 14)),  # 16:59 New York (EDT)
        (utc(2026, 10, 14, 21, 0), date(2026, 10, 15)),  # 17:00 New York: rollover
        (utc(2026, 1, 14, 21, 59), date(2026, 1, 14)),  # winter (EST): 16:59 New York
        (utc(2026, 1, 14, 22, 0), date(2026, 1, 15)),
    ],
)
def test_trading_day_rolls_at_17_new_york(ts: datetime, day: date) -> None:
    assert CAL.trading_day(ts) == day


def test_day_bounds_follow_dst() -> None:
    start, end = CAL.day_bounds(date(2026, 10, 15))
    assert (start, end) == (utc(2026, 10, 14, 21, 0), utc(2026, 10, 15, 21, 0))
    start, end = CAL.day_bounds(date(2026, 1, 15))
    assert (start, end) == (utc(2026, 1, 14, 22, 0), utc(2026, 1, 15, 22, 0))


def test_weekend_and_reopen() -> None:
    assert CAL.is_open(utc(2026, 10, 16, 20, 59))  # Friday 16:59 New York
    assert not CAL.is_open(utc(2026, 10, 16, 21, 0))  # Friday 17:00: closed
    assert not CAL.is_open(utc(2026, 10, 18, 20, 59))  # Sunday 16:59
    assert CAL.is_open(utc(2026, 10, 18, 21, 0))  # Sunday 17:00: open (Monday's day)
    assert CAL.next_open(utc(2026, 10, 17, 12, 0)) == utc(2026, 10, 18, 21, 0)
    assert CAL.next_open(NOW) == NOW


def test_holidays_close_the_whole_trading_day() -> None:
    assert not CAL.is_open(utc(2026, 12, 25, 12, 0))  # Christmas (Friday)
    assert not CAL.is_open(utc(2026, 12, 24, 23, 0))  # already Christmas' trading day
    assert CAL.next_open(utc(2026, 12, 25, 12, 0)) == utc(2026, 12, 27, 22, 0)
    custom = FxCalendar(holidays=frozenset())
    assert custom.is_open(utc(2026, 12, 25, 12, 0))


def test_sessions() -> None:
    assert CAL.active_sessions(utc(2026, 10, 14, 13, 0)) == ["London", "New York"]
    assert "Tokyo" in CAL.active_sessions(utc(2026, 10, 14, 2, 0))
    assert CAL.active_sessions(utc(2026, 10, 17, 12, 0)) == []  # market closed


# --- lifecycle ---------------------------------------------------------------------------


def test_lifecycle_requires_every_stage() -> None:
    s = OrderState.SIGNAL
    for nxt in (
        OrderState.PROPOSAL,
        OrderState.AUTHORIZED,
        OrderState.SENT,
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    ):
        s = transition(s, nxt)
    assert s is OrderState.FILLED
    assert not can_transition(OrderState.SIGNAL, OrderState.FILLED)
    assert not can_transition(OrderState.PROPOSAL, OrderState.SENT)  # never without risk
    assert can_transition(OrderState.SENT, OrderState.UNKNOWN)
    assert can_transition(OrderState.UNKNOWN, OrderState.FILLED)
    with pytest.raises(IllegalTransitionError):
        transition(OrderState.FILLED, OrderState.SENT)
