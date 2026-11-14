"""Trend policy: exits never depend on volatility; unusable volatility blocks only buys/resizes."""

import numpy as np
import pandas as pd

from app.trend.costs import CostModel
from app.trend.signal import CASH, LONG
from app.trend.simulator import Context, simulate
from app.trend.strategy import TrendPolicy, signal_table

DAY = pd.Timestamp("2024-03-01")
RATE = pd.Series([0.0], index=pd.DatetimeIndex(["2019-12-31"]))
FREE = CostModel(0.0, 0.0, None, {"A": 0.0}, {"A": 0.0})


def _policy(state: str, vol: float, stale: bool, last: str) -> TrendPolicy:
    table = pd.DataFrame({"state": [state], "vol": [vol], "stale": [stale]}, index=[DAY])
    policy = TrendPolicy({"A": table}, ("A",))
    policy.last_state["A"] = last
    return policy


def _ctx(units: int) -> Context:
    return Context(DAY, None, False, 1000.0, {"A": units}, {"A": 10.0}, frozenset())


def test_exit_with_stale_volatility_still_sells() -> None:
    for vol, stale in ((float("nan"), False), (0.0, False), (0.15, True)):
        policy = _policy(CASH, vol, stale, LONG)
        assert policy.decide(_ctx(units=30)) == {"A": 0.0}
        assert policy.last_state["A"] == CASH


def test_unusable_volatility_blocks_buys_and_resizes_only() -> None:
    entry = _policy(LONG, float("nan"), False, CASH)
    assert entry.decide(_ctx(units=0)) == {}
    assert entry.flags and entry.last_state["A"] == CASH  # state not updated
    resize = _policy(LONG, 0.15, True, LONG)
    assert resize.decide(_ctx(units=1)) == {}  # far below target, but no resize allowed


def test_position_with_stale_data_and_turn_to_cash_generates_the_sale() -> None:
    """End to end: hold A, then 7 identical closes (6 zero returns: stale window), then a
    collapse below the SMA200. The CASH decision must send the sale and it must fill."""
    rng = np.random.default_rng(5)
    n_up = 270
    rising = 10 * np.cumprod(1 + 0.004 + rng.normal(0, 0.004, n_up))
    flat = np.full(7, rising[-1])
    values = np.concatenate([rising, flat, [rising[-1] * 0.2], [rising[-1] * 0.2]])
    days = pd.bdate_range("2023-01-02", periods=len(values))
    prices = {"A": pd.Series(values, index=days)}
    table = signal_table(prices["A"])
    drop_day = days[n_up + 7]
    assert table.loc[drop_day, "state"] == CASH and bool(table.loc[drop_day, "stale"])
    policy = TrendPolicy({"A": table}, ("A",))
    result = simulate(prices, days, RATE, FREE, policy, 10_000.0, days[210], days[-1])
    buys = [t for t in result.trades if t["quantity"] > 0]
    sells = [t for t in result.trades if t["quantity"] < 0]
    assert buys, "the rising phase must open a position"
    assert [(t["decided"], t["date"]) for t in sells] == [(drop_day, days[-1])]
    assert result.daily["holdings"].iloc[-1] == 0.0
