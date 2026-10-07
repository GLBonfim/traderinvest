"""Exact accounting: positions, trades, costs, equity reconstruction, metrics, benchmarks."""

import dataclasses
import math

import numpy as np
import pandas as pd
import pytest

from app.backtest.config import BacktestConfig
from app.backtest.engine import BENCHMARK_PRICE, BENCHMARK_TOTAL, Backtester
from app.backtest.execution import buy_notional, order_costs
from app.backtest.metrics import drawdown, return_metrics, returns_from_equity, trade_metrics
from app.backtest.portfolio import target_positions
from tests.unit.backtest.helpers import FREE, bars_from, states

L, F, INS = "LONG", "FLAT", "INSUFFICIENT_DATA"


# ── target policy ──


def test_insufficient_data_never_opens_or_closes() -> None:
    np.testing.assert_array_equal(
        target_positions([INS, INS, L, INS, F, INS, INS]), [0, 0, 1, 1, 0, 0, 0]
    )
    with pytest.raises(ValueError):
        target_positions(["SHORT"])


# ── exact example without costs ──


def exact_case() -> tuple[pd.DataFrame, pd.Series]:
    bars = bars_from(opens=[100, 100, 110, 120, 125], closes=[100, 110, 121, 120, 130])
    return bars, states(bars, [L, L, F, F, F])


def test_exact_example_without_costs() -> None:
    bars, st = exact_case()
    r = Backtester(FREE).run_states(bars, st, strategy_id="x")
    eq = r.equity
    # decision LONG at close of bar 0 -> buy 1 share at open of bar 1 (100);
    # decision FLAT at close of bar 2 -> sell at open of bar 3 (120)
    assert eq["position"].tolist() == [0, 1, 1, 0, 0]
    np.testing.assert_allclose(eq["equity"], [100, 110, 121, 120, 120])
    np.testing.assert_allclose(eq["daily_return"], [0, 0.1, 0.1, 120 / 121 - 1, 0])
    t = r.trades.iloc[0]
    assert (t["entry_price"], t["exit_price"]) == (100, 120)
    assert t["entry_time"] == bars.index[1] and t["exit_time"] == bars.index[3]
    assert t["gross_return"] == pytest.approx(0.2) and t["net_return"] == pytest.approx(0.2)
    assert t["holding_sessions"] == 2
    assert r.open_position is None and r.pending_state is None
    assert r.metrics["cumulative_return"] == pytest.approx(0.2)
    assert r.metrics["completed_trades"] == 1 and r.metrics["win_rate"] == 1.0


@pytest.mark.parametrize(
    ("seq", "orders"),
    [
        ([L, L, L, L, L], 1),
        ([F, F, F, F, F], 0),
        ([INS, INS, INS, INS, INS], 0),
        ([F, L, L, F, F], 2),
        ([L, F, L, F, F], 4),
        ([INS, L, INS, F, INS], 2),
        ([INS, L, INS, INS, F], 1),
    ],
)
def test_orders_only_on_effective_position_changes(seq: list[str], orders: int) -> None:
    bars, _ = exact_case()
    r = Backtester(FREE).run_states(bars, states(bars, seq), strategy_id="x")
    assert r.metrics["orders"] == orders
    assert len(r.fills) == orders
    if seq == [INS, L, INS, INS, F]:  # FLAT decided on the LAST bar: no next session -> pending
        assert r.pending_state is not None and r.pending_state["state"] == F


def test_open_position_is_marked_not_exited() -> None:
    bars, _ = exact_case()
    r = Backtester(FREE).run_states(bars, states(bars, [L] * 5), strategy_id="x")
    assert r.trades.empty and r.metrics["completed_trades"] == 0
    op = r.open_position
    assert op is not None and op["status"] == "open_marked_to_market"
    assert op["mark_price"] == 130 and op["unrealized_gross_pnl"] == pytest.approx(30)
    assert math.isnan(r.metrics["win_rate"])
    assert r.metrics["realized_pnl"] == 0 and r.metrics["unrealized_pnl"] == pytest.approx(30)


def test_final_decision_is_pending_not_executed() -> None:
    bars, _ = exact_case()
    r = Backtester(FREE).run_states(bars, states(bars, [F, F, F, F, L]), strategy_id="x")
    assert r.fills.empty
    assert r.pending_state is not None and r.pending_state["state"] == L
    assert r.pending_state["status"] == "not_executed_no_next_session_in_data"


# ── costs ──

COSTLY = BacktestConfig(
    initial_capital=10_000.0, commission_per_trade=1.0, spread_bps=10.0, slippage_bps=5.0
)


def test_order_costs_and_buy_notional() -> None:
    c = order_costs(10_000.0, COSTLY)
    assert (c.commission, c.spread, c.slippage) == pytest.approx((1.0, 5.0, 5.0))
    n = buy_notional(10_000.0, COSTLY)
    assert n + order_costs(n, COSTLY).total == pytest.approx(10_000.0)
    with_min = BacktestConfig(
        commission_per_trade=0.0, minimum_commission=5.0, spread_bps=0, slippage_bps=0
    )
    assert buy_notional(1000.0, with_min) == pytest.approx(995.0)
    with pytest.raises(ValueError):
        buy_notional(0.5, COSTLY)


def test_costs_reconcile_everywhere() -> None:
    bars, _ = exact_case()
    r = Backtester(COSTLY).run_states(bars, states(bars, [L, F, L, L, L]), strategy_id="x")
    fills_total = r.fills["total_cost"].sum()
    assert r.equity["costs"].sum() == pytest.approx(fills_total)
    assert r.metrics["total_costs"] == pytest.approx(fills_total)
    open_costs = r.open_position["entry_costs"] if r.open_position else 0.0
    assert r.trades["total_cost"].sum() + open_costs == pytest.approx(fills_total)
    t = r.trades.iloc[0]
    assert t["net_pnl"] == pytest.approx(t["gross_pnl"] - t["total_cost"])
    assert t["total_cost"] == pytest.approx(t["commission"] + t["spread_cost"] + t["slippage_cost"])
    # realized + unrealized == total P&L
    m = r.metrics
    assert m["realized_pnl"] + m["unrealized_pnl"] == pytest.approx(m["total_pnl"])
    assert m["unrealized_pnl"] == pytest.approx(r.open_position["unrealized_net_pnl"])


def test_gross_track_ignores_costs_and_net_is_lower() -> None:
    bars, st = exact_case()
    no_costs = dataclasses.replace(
        COSTLY, commission_per_trade=0.0, spread_bps=0.0, slippage_bps=0.0
    )
    free = Backtester(no_costs).run_states(bars, st, strategy_id="x")
    costly = Backtester(COSTLY).run_states(bars, st, strategy_id="x")
    np.testing.assert_allclose(costly.equity["gross_equity"], free.equity["equity"])
    assert costly.metrics["cumulative_return"] < costly.metrics["gross_cumulative_return"]
    assert costly.metrics["cost_drag_cumulative"] > 0


def test_equity_reconstructs_from_cash_and_shares() -> None:
    bars, _ = exact_case()
    r = Backtester(COSTLY).run_states(bars, states(bars, [L, F, L, L, F]), strategy_id="x")
    eq = r.equity
    np.testing.assert_allclose(eq["equity"], eq["cash"] + eq["shares"] * eq["close"])
    compounded = (1 + eq["daily_return"]).prod() - 1
    assert compounded == pytest.approx(r.metrics["cumulative_return"])


# ── metrics ──


def test_return_metrics_exact() -> None:
    eq = pd.Series([101.0, 99.99, 102.0, 102.0])
    r = returns_from_equity(eq, 100.0)
    np.testing.assert_allclose(r, [0.01, 99.99 / 101 - 1, 102 / 99.99 - 1, 0.0])
    m = return_metrics(r, eq, 100.0, 252, 0.0)
    assert m["cumulative_return"] == pytest.approx(0.02)
    assert m["cagr"] == pytest.approx(1.02 ** (252 / 4) - 1)
    assert m["annualized_volatility"] == pytest.approx(r.std(ddof=1) * math.sqrt(252))
    assert m["sharpe"] == pytest.approx(r.mean() / r.std(ddof=1) * math.sqrt(252))
    downside = math.sqrt((r.clip(upper=0) ** 2).mean())
    assert m["sortino"] == pytest.approx(r.mean() / downside * math.sqrt(252))


def test_undefined_metrics_are_nan() -> None:
    flat = pd.Series([100.0] * 5)
    m = return_metrics(returns_from_equity(flat, 100.0), flat, 100.0, 252, 0.0)
    assert math.isnan(m["sharpe"]) and math.isnan(m["sortino"])  # zero volatility
    rising = pd.Series([101.0, 102.0, 103.0])
    m2 = return_metrics(returns_from_equity(rising, 100.0), rising, 100.0, 252, 0.0)
    assert math.isnan(m2["sortino"])  # no downside
    empty = pd.Series([], dtype=float)
    m3 = return_metrics(empty, empty, 100.0, 252, 0.0)
    assert math.isnan(m3["cagr"]) and math.isnan(m3["cumulative_return"])
    t = trade_metrics(pd.DataFrame(columns=["net_return", "net_pnl", "holding_sessions"]))
    assert t["completed_trades"] == 0 and math.isnan(t["win_rate"])


def test_drawdown_and_duration() -> None:
    eq = pd.Series([100.0, 110.0, 99.0, 105.0, 120.0, 118.0])
    dd = drawdown(eq, 100.0)
    np.testing.assert_allclose(dd["drawdown"], [0, 0, -0.1, 105 / 110 - 1, 0, 118 / 120 - 1])
    m = return_metrics(returns_from_equity(eq, 100.0), eq, 100.0, 252, 0.0)
    assert m["max_drawdown"] == pytest.approx(-0.1)
    assert m["max_drawdown_duration_sessions"] == 2
    first_loss = drawdown(pd.Series([90.0]), 100.0)  # peak includes the initial capital
    assert first_loss["drawdown"].iloc[0] == pytest.approx(-0.1)


def test_trade_metrics_and_turnover() -> None:
    bars, _ = exact_case()
    r = Backtester(FREE).run_states(bars, states(bars, [L, F, L, F, F]), strategy_id="x")
    t = r.trades
    assert len(t) == 2
    assert r.metrics["win_rate"] == pytest.approx((t["net_return"] > 0).mean())
    assert r.metrics["avg_holding_sessions"] == pytest.approx(t["holding_sessions"].mean())
    expected_turnover = r.equity["traded_notional"].sum() / r.equity["equity"].mean()
    assert r.metrics["turnover"] == pytest.approx(expected_turnover)
    assert r.metrics["exposure"] == pytest.approx(r.equity["position"].mean())


# ── benchmarks ──


def test_price_vs_total_return_benchmarks() -> None:
    bars = bars_from(opens=[100, 100, 100, 100], closes=[100, 100, 100, 100])
    bars["adj_close"] = [96.0, 97.0, 98.0, 100.0]  # dividends reflected in the adjusted series
    out = Backtester(FREE).benchmarks(bars)
    price, total = out[BENCHMARK_PRICE], out[BENCHMARK_TOTAL]
    assert price.metrics["cumulative_return"] == pytest.approx(0.0)
    # entry at adjusted open of bar 1 (100 * 97/100 = 97), valued at adj close 100
    assert total.metrics["cumulative_return"] == pytest.approx(100 / 97 - 1)


def test_total_benchmark_absent_without_adjusted_data() -> None:
    bars, _ = exact_case()
    out = Backtester(FREE).benchmarks(bars.assign(adj_close=np.nan))
    assert BENCHMARK_TOTAL not in out and BENCHMARK_PRICE in out


def test_trade_table_schema_is_fixed() -> None:
    from app.backtest.models import TRADE_COLUMNS

    bars, _ = exact_case()
    with_trade = Backtester(FREE).run_states(bars, states(bars, [L, F, F, F, F]), strategy_id="x")
    without = Backtester(FREE).run_states(bars, states(bars, [F] * 5), strategy_id="x")
    assert tuple(with_trade.trades.columns) == TRADE_COLUMNS == tuple(without.trades.columns)
    assert tuple(with_trade.trades.iloc[0].index) == TRADE_COLUMNS
