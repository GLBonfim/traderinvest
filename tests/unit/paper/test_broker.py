"""PaperBroker: execution timing, costs, fractional quantities, rejections, safety, no network."""

import ast
from pathlib import Path

import pandas as pd
import pytest

from app.backtest.config import BacktestConfig
from app.backtest.execution import buy_notional, order_costs
from app.paper.broker import PaperBroker
from app.paper.config import PaperConfig
from app.paper.models import FILLED, REJECTED, Instruction
from app.risk.manager import PlannedOrder

T0 = pd.Timestamp("2024-07-01 20:00", tz="UTC")  # close of the decision session
S1 = pd.Timestamp("2024-07-02 13:30", tz="UTC")  # next session open
S2 = pd.Timestamp("2024-07-03 13:30", tz="UTC")
CFG = PaperConfig(backtest=BacktestConfig(initial_capital=10_000.0))


def ins(target: float, observed: pd.Timestamp = T0, scheduled: pd.Timestamp = S1, req: float = 1.0) -> Instruction:
    return Instruction("D1", observed, scheduled, req, target)


def test_fill_at_scheduled_open_with_phase8_costs() -> None:
    b = PaperBroker(CFG)
    res = b.execute(ins(1.0), session_ts=S1, open_price=123.45, band=0.1, id_prefix="r")
    assert res.order is not None and res.order.status == FILLED and res.fill is not None
    n = buy_notional(10_000.0, CFG.backtest)
    c = order_costs(n, CFG.backtest)
    assert res.fill.executed_at == S1 and res.fill.price == 123.45
    assert res.fill.quantity == pytest.approx(n / 123.45) and res.fill.quantity % 1 != 0  # fractional
    assert (res.fill.commission, res.fill.spread_cost, res.fill.slippage_cost) == (c.commission, c.spread, c.slippage)
    assert b.cash == pytest.approx(0.0, abs=1e-6) and b.cumulative_costs == c.total
    assert res.order.order_id == "r-O000001" and res.fill.order_id == res.order.order_id


def test_execution_not_after_decision_is_rejected() -> None:
    b = PaperBroker(CFG)
    res = b.execute(ins(1.0, observed=S1, scheduled=S1), session_ts=S1, open_price=100.0, band=0.1, id_prefix="r")
    assert res.order is not None and res.order.status == REJECTED
    assert res.order.rejection_reason == "execution_not_after_decision" and res.fill is None
    assert b.shares == 0 and b.cash == 10_000.0


def test_missing_scheduled_session_is_rejected() -> None:
    b = PaperBroker(CFG)
    res = b.execute(ins(1.0, scheduled=S1), session_ts=S2, open_price=100.0, band=0.1, id_prefix="r")
    assert res.order is not None and res.order.rejection_reason == "scheduled_session_missing_in_data"
    assert b.shares == 0


def test_sessions_must_increase() -> None:
    b = PaperBroker(CFG)
    b.execute(ins(0.0, scheduled=S2), session_ts=S2, open_price=100.0, band=0.1, id_prefix="r")
    with pytest.raises(ValueError, match="increasing time order"):
        b.execute(ins(0.0, scheduled=S1), session_ts=S1, open_price=100.0, band=0.1, id_prefix="r")


@pytest.mark.parametrize(("target", "reason"), [(1.5, "exceeds_max_gross_exposure"), (-0.5, "short_not_allowed")])
def test_leverage_and_shorting_rejected(target: float, reason: str) -> None:
    b = PaperBroker(CFG)
    if target < 0:  # need a position for a sell to be planned
        b.execute(ins(1.0), session_ts=S1, open_price=100.0, band=0.1, id_prefix="r")
        res = b.execute(ins(target, scheduled=S2), session_ts=S2, open_price=100.0, band=0.1, id_prefix="r")
    else:
        res = b.execute(ins(target), session_ts=S1, open_price=100.0, band=0.1, id_prefix="r")
    assert res.order is not None and res.order.status == REJECTED and res.order.rejection_reason == reason


def test_insufficient_cash_rejected() -> None:
    b = PaperBroker(CFG)
    planned = PlannedOrder("buy", 20_000.0, False)
    assert b._safety_check(ins(1.0), planned, S1, 100.0) == "insufficient_cash"


def test_no_order_when_within_band_or_flat() -> None:
    b = PaperBroker(CFG)
    res = b.execute(ins(0.0), session_ts=S1, open_price=100.0, band=0.1, id_prefix="r")
    assert res.order is None and res.no_order_reason == "already_flat"
    b2 = PaperBroker(CFG)
    b2.execute(ins(1.0), session_ts=S1, open_price=100.0, band=0.1, id_prefix="r")
    res2 = b2.execute(ins(0.95, scheduled=S2), session_ts=S2, open_price=100.0, band=0.1, id_prefix="r")
    assert res2.order is None and res2.no_order_reason == "within_rebalance_band"


def test_average_cost_realized_and_unrealized() -> None:
    free = PaperConfig(backtest=BacktestConfig(initial_capital=1000.0, commission_per_trade=0.0,
                                               spread_bps=0.0, slippage_bps=0.0))  # fmt: skip
    b = PaperBroker(free)
    b.execute(ins(1.0), session_ts=S1, open_price=100.0, band=0.1, id_prefix="r")  # 10 shares @100
    assert b.shares == pytest.approx(10) and b.avg_cost == pytest.approx(100)
    b.execute(ins(0.5, scheduled=S2), session_ts=S2, open_price=120.0, band=0.1, id_prefix="r")
    # equity 1200 -> target 600 -> sell 5 shares @120: realized (120 - 100) x 5 = 100
    assert b.shares == pytest.approx(5) and b.realized_pnl == pytest.approx(100)
    assert b.equity(130.0) == pytest.approx(1000 + 100 + (130 - 100) * 5)


def test_only_instructions_are_accepted() -> None:
    with pytest.raises(TypeError, match="risk-approved"):
        PaperBroker(CFG).execute("LONG", session_ts=S1, open_price=100.0, band=0.1, id_prefix="r")  # type: ignore[arg-type]


def test_deterministic_fills() -> None:
    def go() -> tuple[float, float]:
        b = PaperBroker(CFG)
        r = b.execute(ins(1.0), session_ts=S1, open_price=101.0, band=0.1, id_prefix="r")
        assert r.fill is not None
        return r.fill.quantity, b.cash

    assert go() == go()


def test_paper_module_has_no_network_or_broker_code() -> None:
    forbidden = {"requests", "httpx", "socket", "urllib", "aiohttp", "websocket", "alpaca", "ib_insync", "ibapi"}
    for path in Path("app/paper").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) else [])  # fmt: skip
            assert not {n.split(".")[0] for n in names} & forbidden, path


def test_config_rejects_leverage() -> None:
    with pytest.raises(ValueError):
        PaperConfig(max_gross_exposure=1.5)
