from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.fx.ibkr.budget_settings import (
    DEFAULT,
    KEY,
    BudgetRiskManager,
    apply_settings,
    load_settings,
    validate,
)
from app.fx.ibkr.pilot import PilotConfig, PilotState
from app.fx.models import Side
from app.fx.risk import RiskConfig, RiskDecision, RiskManager, RiskState
from app.fx.store import FxStore


@pytest.mark.parametrize(
    "capital,order",
    [(0, 1000), (10001, 1000), (float("nan"), 1000), (500, 100), (500, 1500), (500, 6000)],
)
def test_settings_reject_unsupported_sizes(capital, order):
    with pytest.raises(ValueError):
        validate({"capital_eur": capital, "order_eur": order, "fixed_order": True})


def pilot(store):
    state = PilotState(active_ids=["one"])
    state.realized = -12.0
    state.returns = {"one": [-0.3, 0.8]}
    risk = BudgetRiskManager(
        RiskConfig(),
        RiskState(day_start_equity=10000, last_equity=9988, peak_equity=10005, entries_used=3),
    )
    lab = SimpleNamespace(store=store, state=state, risk=risk, config=PilotConfig())
    lab.save = lambda: store.put_states({"pilot": state.to_dict(), "risk": risk.state.to_dict()})
    return lab


def test_budget_change_preserves_losses_counters_and_peak_gap(tmp_path):
    store = FxStore(tmp_path / "db.sqlite")
    try:
        lab = pilot(store)
        value = {"capital_eur": 500, "order_eur": 1000, "fixed_order": True}
        applied = apply_settings(lab, value, "a" * 32)
        assert lab.state.realized == -12 and lab.state.returns == {"one": [-0.3, 0.8]}
        assert lab.risk.state.entries_used == 3
        assert lab.risk.state.last_equity == 488 and lab.risk.state.peak_equity == 505
        assert lab.risk.state.day_start_equity == 500
        assert lab.config.capital == 500 and lab.risk.desired_units == 1000
        assert load_settings(store) == applied
        count = store.count()
        assert apply_settings(lab, value, "a" * 32) == applied and store.count() == count
        assert store.verify() == (True, None)
        store.put_state(KEY, {**applied, "capital_eur": 1000})
        with pytest.raises(ValueError, match="difere"):
            load_settings(store)
    finally:
        store.close()


@pytest.mark.parametrize("blocked", ["open", "pending", "reservation", "kill", "lock"])
def test_budget_waits_and_never_clears_blocks(tmp_path, blocked):
    store = FxStore(tmp_path / "db.sqlite")
    try:
        lab = pilot(store)
        if blocked == "open":
            lab.state.open["entry"] = {}
        if blocked == "pending":
            lab.state.pending["entry"] = {}
        if blocked == "reservation":
            lab.risk.state.reservations["x"] = "EURUSD"
        if blocked == "kill":
            lab.risk.state.kill_switch = True
        if blocked == "lock":
            lab.risk.state.lock = "daily_loss"
        before = lab.risk.state.to_dict()
        with pytest.raises(ValueError):
            apply_settings(lab, DEFAULT, "b" * 32)
        assert lab.risk.state.to_dict() == before and store.count() == 0
    finally:
        store.close()


def test_fixed_size_cannot_increase_risk_authorized_size(monkeypatch):
    result = RiskDecision(True, True, Side.SELL, 3000, (), "allowed")
    monkeypatch.setattr(RiskManager, "evaluate", lambda *args, **kwargs: result)
    risk = BudgetRiskManager(RiskConfig())
    assert risk.evaluate() == result
    risk.desired_units = 1000
    assert risk.evaluate().units == 1000
    risk.desired_units = 4000
    assert not risk.evaluate().approved and risk.evaluate().units == 0
    assert risk.evaluate().refusals[0].code == "chosen_order_budget"
    result = replace(result, approved=False)
    assert risk.evaluate() == result


def test_smaller_budget_rechecks_drawdown_without_erasing_losses(tmp_path):
    from datetime import UTC, datetime

    store = FxStore(tmp_path / "db.sqlite")
    try:
        lab = pilot(store)
        lab.risk.config = replace(lab.risk.config, max_drawdown_pct=5, daily_loss_limit_pct=1)
        now = datetime.now(UTC)
        lab.risk.state.trading_day = lab.risk.calendar.trading_day(now).isoformat()
        apply_settings(lab, {"capital_eur": 200, "order_eur": 1000, "fixed_order": True}, "c" * 32)
        lab.risk.observe(188, now)
        assert lab.risk.state.kill_switch
        assert lab.risk.state.lock == "daily_loss"
        assert lab.state.realized == -12 and lab.risk.state.entries_used == 3
    finally:
        store.close()
