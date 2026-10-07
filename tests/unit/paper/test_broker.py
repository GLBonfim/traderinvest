"""PaperBroker: execution timing, costs, fractional quantities, rejections, safety, no network."""

import ast
from pathlib import Path

import pandas as pd
import pytest

from app.backtest.config import BacktestConfig
from app.backtest.execution import buy_notional, order_costs
from app.core.config import TradingMode
from app.core.safety import RealMoneyExecutionBlockedError, refuse_real_money_order
from app.paper.broker import PaperBroker
from app.paper.config import PaperConfig
from app.paper.models import ENTRY, EXIT, FILLED, REDUCE, REJECTED, Instruction
from app.risk.manager import PlannedOrder

T0 = pd.Timestamp("2024-07-01 20:00", tz="UTC")  # close of the decision session
S1 = pd.Timestamp("2024-07-02 13:30", tz="UTC")  # next session open
S2 = pd.Timestamp("2024-07-03 13:30", tz="UTC")
CFG = PaperConfig(backtest=BacktestConfig(initial_capital=10_000.0))
FREE = PaperConfig(
    backtest=BacktestConfig(
        initial_capital=1000.0, commission_per_trade=0.0, spread_bps=0.0, slippage_bps=0.0
    )
)


def ins(
    target: float, observed: pd.Timestamp = T0, scheduled: pd.Timestamp = S1, req: float = 1.0
) -> Instruction:
    return Instruction("D1", observed, scheduled, req, target, "entry", 2.0)


def ex(b: PaperBroker, i: Instruction, ts: pd.Timestamp, price: float, n: int = 1):  # type: ignore[no-untyped-def]
    return b.execute(
        i, session_ts=ts, open_price=price, band=0.1, order_id=f"O{n}", fill_id=f"F{n}"
    )


def test_fill_at_scheduled_open_with_phase8_costs() -> None:
    b = PaperBroker(CFG)
    res = ex(b, ins(1.0), S1, 123.45)
    assert res.order is not None and res.order.status == FILLED and res.fill is not None
    n = buy_notional(10_000.0, CFG.backtest)
    c = order_costs(n, CFG.backtest)
    assert res.fill.executed_at == S1 and res.fill.price == 123.45
    assert res.fill.quantity == pytest.approx(n / 123.45) and res.fill.quantity % 1 != 0
    assert (res.fill.commission, res.fill.spread_cost, res.fill.slippage_cost) == (
        c.commission,
        c.spread,
        c.slippage,
    )
    assert b.cash == pytest.approx(0.0, abs=1e-6) and b.account.cumulative_costs == c.total
    assert res.order.order_id == "O1" and res.fill.order_id == res.order.order_id
    assert res.order.action == ENTRY and res.fill.action == ENTRY
    assert res.fill.cash_after == b.cash and res.fill.position_after == b.shares


def test_execution_not_after_decision_is_rejected() -> None:
    b = PaperBroker(CFG)
    res = ex(b, ins(1.0, observed=S1, scheduled=S1), S1, 100.0)
    assert res.order is not None and res.order.status == REJECTED
    assert res.order.rejection_reason == "execution_not_after_decision" and res.fill is None
    assert b.shares == 0 and b.cash == 10_000.0


def test_missing_scheduled_session_is_rejected() -> None:
    b = PaperBroker(CFG)
    res = ex(b, ins(1.0, scheduled=S1), S2, 100.0)
    assert res.order is not None
    assert res.order.rejection_reason == "scheduled_session_missing_in_data"
    assert b.shares == 0


def test_sessions_must_increase() -> None:
    b = PaperBroker(CFG)
    ex(b, ins(0.0, scheduled=S2), S2, 100.0)
    with pytest.raises(ValueError, match="increasing time order"):
        ex(b, ins(0.0, scheduled=S1), S1, 100.0)


@pytest.mark.parametrize(
    ("target", "reason"), [(1.5, "exceeds_max_gross_exposure"), (-0.5, "short_not_allowed")]
)
def test_leverage_and_shorting_rejected(target: float, reason: str) -> None:
    b = PaperBroker(CFG)
    if target < 0:  # need a position for a sell to be planned
        ex(b, ins(1.0), S1, 100.0)
        res = ex(b, ins(target, scheduled=S2), S2, 100.0, 2)
    else:
        res = ex(b, ins(target), S1, 100.0)
    assert res.order is not None and res.order.status == REJECTED
    assert res.order.rejection_reason == reason


def test_insufficient_cash_rejected() -> None:
    b = PaperBroker(CFG)
    planned = PlannedOrder("buy", 20_000.0, False)
    assert b._safety_check(ins(1.0), planned, S1, 100.0) == "insufficient_cash"


def test_no_order_when_within_band_or_flat() -> None:
    b = PaperBroker(CFG)
    res = ex(b, ins(0.0), S1, 100.0)
    assert res.order is None and res.no_order_reason == "already_flat"
    b2 = PaperBroker(CFG)
    ex(b2, ins(1.0), S1, 100.0)
    res2 = ex(b2, ins(0.95, scheduled=S2), S2, 100.0, 2)
    assert res2.order is None and res2.no_order_reason == "within_rebalance_band"


def test_average_cost_realized_and_unrealized_exact() -> None:
    b = PaperBroker(FREE)
    ex(b, ins(1.0), S1, 100.0)  # 10 shares @100, basis 1000
    assert b.shares == 10 and b.account.cost_basis == 1000
    r = ex(b, ins(0.5, scheduled=S2), S2, 120.0, 2)
    # equity 1200 -> target 600 -> sell 5 shares @120: realized (600 - 500) - 0 = 100
    assert r.fill is not None and r.fill.action == REDUCE and r.fill.realized_pnl == 100
    assert b.shares == 5 and b.account.realized_pnl == 100 and b.account.cost_basis == 500
    acct = b.account
    assert acct.unrealized_pnl(130.0) == 5 * 130 - 500 == 150
    assert acct.equity(130.0) - 1000 == acct.realized_pnl + acct.unrealized_pnl(130.0) == 250


def test_costs_are_capitalised_and_realized_on_exit() -> None:
    cfg = PaperConfig(
        backtest=BacktestConfig(
            initial_capital=1001.0, commission_per_trade=1.0, spread_bps=0.0, slippage_bps=0.0
        )
    )
    b = PaperBroker(cfg)
    ex(b, ins(1.0), S1, 100.0)  # notional 1000 + 1 commission
    assert b.shares == pytest.approx(10) and b.account.cost_basis == 1001
    assert b.account.unrealized_pnl(100.0) == pytest.approx(-1.0)  # entry costs, unrealized
    r = ex(b, ins(0.0, scheduled=S2), S2, 110.0, 2)
    assert r.fill is not None and r.fill.action == EXIT
    # Phase 8 net_pnl: (1100 - 1000) - (1 + 1) = 98
    assert b.account.realized_pnl == pytest.approx(98) and b.account.cost_basis == 0
    assert b.cash == pytest.approx(1001 + 98)


def test_only_instructions_are_accepted() -> None:
    with pytest.raises(TypeError, match="risk-approved"):
        ex(PaperBroker(CFG), "LONG", S1, 100.0)  # type: ignore[arg-type]


def test_deterministic_fills() -> None:
    def go() -> tuple[float, float]:
        b = PaperBroker(CFG)
        r = ex(b, ins(1.0), S1, 101.0)
        assert r.fill is not None
        return r.fill.quantity, b.cash

    assert go() == go()


# ── safety: local simulation only ──

FORBIDDEN_MODULES = {
    "requests",
    "httpx",
    "httpx2",
    "socket",
    "ssl",
    "urllib",
    "urllib3",
    "http",
    "aiohttp",
    "websocket",
    "websockets",
    "grpc",
    "yfinance",
    "alpaca",
    "alpaca_trade_api",
    "ib_insync",
    "ib_async",
    "ibapi",
    "ccxt",
}


def test_paper_module_has_no_network_or_broker_code() -> None:
    files = sorted(Path("app/paper").glob("*.py"))
    assert files
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not {n.split(".")[0] for n in names} & FORBIDDEN_MODULES, path


def test_trading_modes_and_real_money_blocker_unchanged() -> None:
    assert {m.value for m in TradingMode} == {"disabled", "paper"}
    with pytest.raises(RealMoneyExecutionBlockedError):
        refuse_real_money_order()


def test_config_rejects_leverage() -> None:
    with pytest.raises(ValueError):
        PaperConfig(max_gross_exposure=1.5)
