"""Sizing formulas, stop levels/evaluation, drawdown and session monitors, caps, config."""

import math

import pytest

from app.risk.config import (
    SCENARIOS,
    DrawdownSpec,
    LimitSpec,
    RiskConfig,
    SessionSpec,
    SizingSpec,
    StopSpec,
)
from app.risk.limits import LOCKED, NORMAL, WARNING, DrawdownMonitor, SessionMonitor, cap_exposure
from app.risk.sizing import size_exposure
from app.risk.stops import evaluate_stop, stop_level

NAN = float("nan")

# ── sizing ──


def test_full_and_fixed_fraction() -> None:
    assert size_exposure(SizingSpec("full"), close=100, realized_vol=NAN, atr=NAN) == (1.0, "full")
    assert (
        size_exposure(
            SizingSpec("fixed_fraction", fraction=0.4), close=100, realized_vol=NAN, atr=NAN
        )[0]
        == 0.4
    )


@pytest.mark.parametrize(("vol", "expected"), [(0.20, 0.5), (0.10, 1.0), (0.05, 1.0), (0.40, 0.25)])
def test_volatility_target(vol: float, expected: float) -> None:
    spec = SizingSpec("volatility_target", target_volatility=0.10)
    assert size_exposure(spec, close=100, realized_vol=vol, atr=1)[0] == pytest.approx(expected)


@pytest.mark.parametrize("vol", [0.0, -0.1, NAN, math.inf])
def test_undefined_volatility_never_defaults(vol: float) -> None:
    spec = SizingSpec("volatility_target")
    assert size_exposure(spec, close=100, realized_vol=vol, atr=1) == (0.0, "risk_input_undefined")


def test_atr_risk_sizing() -> None:
    spec = SizingSpec("atr_risk", atr_risk_budget=0.01, atr_stop_multiple=2.0)
    # 1% of equity lost if price falls 2 ATR: exposure = 0.01 * 100 / (2 * 1) = 0.5
    assert size_exposure(spec, close=100, realized_vol=NAN, atr=1.0)[0] == pytest.approx(0.5)
    assert size_exposure(spec, close=100, realized_vol=NAN, atr=0.1)[0] == 1.0  # capped
    for bad in (0.0, NAN, -1.0):
        assert size_exposure(spec, close=100, realized_vol=NAN, atr=bad) == (
            0.0,
            "risk_input_undefined",
        )


# ── stops ──


def test_stop_levels() -> None:
    assert stop_level(
        StopSpec("fixed_pct", pct=0.1), entry_price=100, atr_at_decision=NAN
    ) == pytest.approx(90)
    assert stop_level(StopSpec("atr", atr_multiple=3), entry_price=100, atr_at_decision=2) == 94
    assert math.isnan(stop_level(StopSpec("atr"), entry_price=100, atr_at_decision=NAN))
    assert math.isnan(stop_level(StopSpec("none"), entry_price=100, atr_at_decision=2))


@pytest.mark.parametrize(
    ("low", "close", "triggered", "touch"),
    [
        (95.0, 97.0, False, False),  # not touched
        (
            89.0,
            92.0,
            False,
            True,
        ),  # low below level, close above: touch only (no intraday inference)
        (88.0, 90.0, True, False),  # close exactly at the level: triggered (<=)
        (85.0, 86.0, True, False),  # close below: triggered
    ],
)
def test_stop_is_evaluated_on_the_close(
    low: float, close: float, triggered: bool, touch: bool
) -> None:
    assert evaluate_stop(90.0, low=low, close=close) == (triggered, touch)


def test_undefined_stop_never_triggers() -> None:
    assert evaluate_stop(NAN, low=1.0, close=1.0) == (False, False)


# ── drawdown monitor ──


def test_drawdown_states_lock_and_unlock() -> None:
    m = DrawdownMonitor(
        DrawdownSpec(enabled=True, warning=0.10, lock=0.20, cooldown_sessions=2), hwm=100.0
    )
    assert m.update(95.0) == pytest.approx(-0.05) and m.state == NORMAL
    m.update(89.0)
    assert m.state == WARNING
    m.update(79.0)
    assert m.state == LOCKED and m.lock_remaining == 2
    m.update(70.0)
    assert m.state == LOCKED
    m.update(72.0)  # cooldown over: unlock and reset the high-water mark
    assert m.state == NORMAL and m.hwm == 72.0
    assert m.max_drawdown == pytest.approx(-0.30)


def test_drawdown_disabled_and_duration() -> None:
    m = DrawdownMonitor(DrawdownSpec(enabled=False), hwm=100.0)
    for e in (90.0, 80.0, 70.0):
        m.update(e)
    assert m.state == NORMAL and m.duration == 3
    m.update(101.0)
    assert m.duration == 0 and m.hwm == 101.0


# ── session monitor ──


def test_session_loss_lock() -> None:
    s = SessionMonitor(
        SessionSpec(enabled=True, max_session_loss=0.03, lock_sessions=1), last_equity=100.0
    )
    assert s.update(98.0) == pytest.approx(-0.02) and not s.locked
    s.update(95.0)  # -3.06%
    assert s.locked
    s.update(95.0)
    assert not s.locked


def test_consecutive_loss_lock() -> None:
    s = SessionMonitor(
        SessionSpec(enabled=True, max_consecutive_losses=2, consecutive_lock_sessions=3),
        last_equity=100.0,
    )
    s.record_round_trip(-1.0)
    assert not s.locked
    s.record_round_trip(-1.0)
    assert s.locked and s.lock_remaining == 3
    s.record_round_trip(+1.0)
    assert s.consecutive_losses == 0


def test_disabled_session_never_locks() -> None:
    s = SessionMonitor(SessionSpec(enabled=False, max_consecutive_losses=1), last_equity=100.0)
    s.update(50.0)
    s.record_round_trip(-10.0)
    assert not s.locked


# ── caps / config ──


def test_exposure_caps() -> None:
    assert cap_exposure(0.9, LimitSpec(max_gross_exposure=0.6), equity=100.0) == (
        0.6,
        "exposure_limit",
    )
    assert cap_exposure(0.9, LimitSpec(max_position_notional=50.0), equity=100.0) == (
        0.5,
        "exposure_limit",
    )
    assert cap_exposure(0.4, LimitSpec(), equity=100.0) == (0.4, None)


@pytest.mark.parametrize(
    "kw",
    [
        {"sizing": SizingSpec(max_exposure=1.5)},  # leverage
        {"limits": LimitSpec(max_gross_exposure=2.0)},  # leverage
        {"sizing": SizingSpec(method="kelly")},
        {"stop": StopSpec(method="trailing")},
        {"drawdown": DrawdownSpec(warning=0.3, lock=0.2)},
        {"rebalance_band": 1.0},
    ],
)
def test_invalid_risk_config(kw: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RiskConfig(**kw)  # type: ignore[arg-type]


def test_predeclared_scenarios() -> None:
    names = [s.name for s in SCENARIOS]
    assert names[0] == "control_no_overlay" and len(names) == 6
    assert len({s.fingerprint() for s in SCENARIOS}) == 6
    assert SCENARIOS[0].sizing.method == "full" and SCENARIOS[0].stop.method == "none"
