from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.trend.research import (
    explain_trades,
    holdings_values,
    json_ready,
    minimum_viable_capital,
    ter_drag_pct,
)
from app.trend.simulator import RunResult
from app.trend.strategy import TrendPolicy

D = pd.Timestamp


def _result(trades: list[dict[str, Any]], days: pd.DatetimeIndex) -> RunResult:
    daily = pd.DataFrame({"nav": np.full(len(days), 1000.0)}, index=days)
    return RunResult(daily, trades, {})


def test_trade_reasons() -> None:
    days = pd.bdate_range("2024-01-01", periods=5)
    trades = [
        {
            "date": days[1],
            "decided": days[0],
            "asset": "A",
            "requested": 10,
            "quantity": 10,
            "status": "filled",
        },
        {
            "date": days[2],
            "decided": days[1],
            "asset": "A",
            "requested": 3,
            "quantity": 3,
            "status": "filled",
        },
        {
            "date": days[3],
            "decided": days[2],
            "asset": "A",
            "requested": -2,
            "quantity": -2,
            "status": "filled",
        },
        {
            "date": days[4],
            "decided": days[3],
            "asset": "A",
            "requested": -11,
            "quantity": -11,
            "status": "filled",
        },
    ]
    policy = TrendPolicy({}, ("A",))
    policy.decisions = [
        {"date": days[0], "asset": "A", "state": "LONG", "w_actual": 0.0},
        {"date": days[1], "asset": "A", "state": "LONG", "w_actual": 0.05},
        {"date": days[2], "asset": "A", "state": "LONG", "w_actual": 0.2},
        {"date": days[3], "asset": "A", "state": "CASH", "w_actual": 0.1},
    ]
    reasons = [r["reason"] for r in explain_trades(_result(trades, days), policy)]
    assert reasons == ["entry: state LONG", "drift: increase", "drift: reduce", "exit: state CASH"]


def test_holdings_and_ter_drag() -> None:
    days = pd.bdate_range("2024-01-01", periods=3)  # Mon, Tue, Wed
    trades = [
        {
            "date": days[1],
            "decided": days[0],
            "asset": "A",
            "requested": 10,
            "quantity": 10,
            "status": "filled",
        }
    ]
    prices = {"A": pd.Series([100.0, 100.0, 100.0], index=days)}
    values = holdings_values(_result(trades, days), prices)
    assert values["A"].tolist() == [0.0, 1000.0, 1000.0]
    drag = ter_drag_pct(values, {"A": 0.365}, _result(trades, days).daily)
    # one day of 1000 EUR at 0.365 %/year = 0.01 EUR over 2/365.25 years of 1000 EUR NAV
    assert drag == pytest.approx(100 * 0.01 / 1000 / (2 / 365.25))


def _part(costs: dict[int, float]) -> dict[str, Any]:
    return {
        "runs": {
            f"strategy|realistic|{c}": {
                "costs": {"commission": v, "spread": 0.0, "slippage": 0.0},
                "average_nav": float(c),
                "years": 1.0,
            }
            for c, v in costs.items()
        }
    }


def test_minimum_viable_capital_pools_both_partitions() -> None:
    dev = _part({500: 10.0, 2000: 12.0, 5000: 15.0, 10000: 20.0, 25000: 30.0})
    val = _part({500: 10.0, 2000: 9.0, 5000: 10.0, 10000: 15.0, 25000: 20.0})
    out = minimum_viable_capital([dev, val])
    assert out["annual_trading_cost_pct"]["2000"] == pytest.approx(100 * 21 / 4000)
    assert out["reference_capital"] == 5000  # 25/10000 = 0.25 % < 0.50 %; 2000 -> 0.525 %
    none = minimum_viable_capital([_part(dict.fromkeys((500, 2000, 5000, 10000, 25000), 1e6))])
    assert none["reference_capital"] is None


def test_json_ready_drops_private_arrays_and_non_finite() -> None:
    raw = {"a": np.float64(1.5), "_excess": np.zeros(3), "b": [float("nan"), np.int64(2)]}
    assert json_ready(raw) == {"a": 1.5, "b": [None, 2]}
