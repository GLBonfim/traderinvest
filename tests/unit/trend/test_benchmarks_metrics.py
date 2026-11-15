from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.trend.benchmarks import CashPolicy, equal_weight, sixty_forty
from app.trend.costs import CostModel
from app.trend.metrics import (
    EDGE,
    INSUFFICIENT,
    NO_EDGE,
    gates,
    max_drawdown,
    sharpe,
    sharpe_uncertainty,
    subperiod_returns,
    summarise,
    verdict,
)
from app.trend.simulator import simulate

RATE = pd.Series([0.0], index=pd.DatetimeIndex(["2019-12-31"]))
FREE = CostModel(0.0, 0.0, None, dict.fromkeys("AB", 0.0), dict.fromkeys("AB", 0.0))


def _flat(days: pd.DatetimeIndex) -> dict[str, pd.Series]:
    return {a: pd.Series(np.full(len(days), 10.0), index=days) for a in "AB"}


def test_equal_weight_buys_first_then_rebalances_each_month_end() -> None:
    days = pd.bdate_range("2024-01-02", "2024-03-29")
    policy = equal_weight(("A", "B"), days)
    r = simulate(_flat(days), days, RATE, FREE, policy, 1000.0, days[0], days[-1])
    filled = [(t["date"], t["asset"], t["quantity"]) for t in r.trades]
    assert filled == [(days[1], "A", 50), (days[1], "B", 50)]  # flat prices: no later change
    decided = sorted({t["decided"] for t in r.trades})
    assert decided == [days[0]]


def test_sixty_forty_rebalances_once_a_year() -> None:
    days = pd.bdate_range("2023-01-02", "2024-12-31")
    prices = _flat(days)
    prices["A"] = pd.Series(np.linspace(10, 20, len(days)), index=days)  # equity drifts up
    policy = sixty_forty({"A": 0.6, "B": 0.4}, days)
    r = simulate(prices, days, RATE, FREE, policy, 10_000.0, days[0], days[-1])
    decided = sorted({t["decided"].date().isoformat() for t in r.trades})
    assert decided == ["2023-01-02", "2023-12-29"]  # start; 2024-12-31 has no later fill
    assert r.pending_at_end and r.pending_at_end[0]["decided"] == pd.Timestamp("2024-12-31")


def test_cash_benchmark_only_earns_interest() -> None:
    days = pd.bdate_range("2024-01-02", periods=5)
    rate = pd.Series([0.036], index=pd.DatetimeIndex(["2023-12-29"]))
    r = simulate({}, days, rate, FREE, CashPolicy(), 1000.0, days[0], days[-1])
    assert r.trades == []
    assert r.daily["nav"].iloc[-1] == pytest.approx(1000 * (1 + 0.0001) ** 2 * 1.0003 * 1.0001)


def test_drawdown_sharpe_and_subperiods() -> None:
    dd, length = max_drawdown(np.array([100, 120, 90, 95, 130, 125.0]))
    assert dd == pytest.approx(0.25) and length == 2
    assert np.isnan(sharpe(np.array([0.01]), 252))
    assert sharpe(np.array([0.01, 0.03]), 4) == pytest.approx(
        0.02 / np.std([0.01, 0.03], ddof=1) * 2
    )
    parts = subperiod_returns(np.array([0.1, 0.1, -0.1, 0.0, 0.0, 0.0, 0.05, 0.05]))
    assert parts == pytest.approx([0.21, -0.1, 0.0, 0.1025])


def _metrics(**kw: Any) -> dict[str, Any]:
    base = {
        "cagr": 0.06,
        "cash_cagr": 0.01,
        "sharpe": 0.6,
        "max_drawdown": 0.12,
        "total_return": 0.5,
        "subperiod_returns": [0.1, 0.0, -0.01, 0.2],
        "pnl_by_asset": {"A": 30.0, "B": 30.0, "C": 40.0, "D": -5.0},
    }
    return base | kw


def test_six_gates_and_verdict() -> None:
    ew = _metrics(sharpe=0.5, max_drawdown=0.18)
    ok = gates(_metrics(), _metrics(total_return=0.01), ew)
    assert all(g.passed for g in ok) and len(ok) == 6
    assert verdict(ok, lower_bound=0.05) == EDGE
    assert verdict(ok, lower_bound=0.0) == INSUFFICIENT
    assert verdict(ok, lower_bound=float("nan")) == INSUFFICIENT
    assert verdict(ok, lower_bound=0.5, evaluation_failed=True) == INSUFFICIENT
    cases = {
        1: (_metrics(cagr=0.01), _metrics(), ew),
        2: (_metrics(sharpe=0.45), _metrics(), _metrics(sharpe=0.5, max_drawdown=0.18)),
        3: (_metrics(max_drawdown=0.201), _metrics(), ew),
        4: (_metrics(), _metrics(total_return=0.0), ew),
        5: (_metrics(subperiod_returns=[0.1, -0.1, -0.01, 0.2]), _metrics(), ew),
        6: (_metrics(pnl_by_asset={"A": 41.0, "B": 59.0}), _metrics(), ew),
    }
    for number, (s, d, e) in cases.items():
        result = gates(s, d, e)
        assert [g.number for g in result if not g.passed] == [number]
        assert verdict(result, lower_bound=1.0) == NO_EDGE
    no_positive = gates(_metrics(pnl_by_asset={"A": -1.0}), _metrics(), ew)
    assert not no_positive[5].passed


def test_bootstrap_is_seeded_and_one_sided() -> None:
    rng = np.random.default_rng(0)
    excess = rng.normal(0.0004, 0.01, 800)
    a = sharpe_uncertainty(excess, 252, benchmark_excess=excess * 0.5)
    b = sharpe_uncertainty(excess, 252, benchmark_excess=excess * 0.5)
    assert a == b
    assert a["lower_one_sided_95"] < sharpe(excess, 252) < a["two_sided_95"]["upper"]
    assert a["two_sided_95"]["lower"] < a["lower_one_sided_95"]
    assert a["two_sided_95"]["n_total"] == 2000


def test_summary_reports_costs_and_pnl() -> None:
    days = pd.bdate_range("2024-01-02", periods=60)
    prices = {
        "A": pd.Series(np.linspace(10, 12, 60), index=days),
        "B": pd.Series(np.linspace(10, 9, 60), index=days),
    }
    costs = CostModel(0.001, 3.0, None, dict.fromkeys("AB", 0.001), dict.fromkeys("AB", 0.0))
    r = simulate(
        prices, days, RATE, costs, equal_weight(("A", "B"), days), 2000.0, days[0], days[-1]
    )
    s = summarise(r, RATE, 2000.0)
    assert s["trades"] >= 2 and s["costs"]["commission"] >= 6.0
    assert s["pnl_by_asset"]["A"] > 0 > s["pnl_by_asset"]["B"]
    assert s["final_nav"] == pytest.approx(r.daily["nav"].iloc[-1])
    assert s["annual_trading_cost_pct"] > 0
