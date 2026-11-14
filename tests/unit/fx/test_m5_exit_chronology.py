"""Synthetic opening chronology; no fits or market data files."""

import pandas as pd
import pytest

from app.fx.research.m5_lesson_training import labels
from app.fx.research.m5_validation import BAR, PROTOCOL, fee_floor, simulate


def scenario(direction, kind):
    idx = pd.date_range("2024-03-04", periods=300, freq="5min", tz="UTC")
    raw = pd.DataFrame(
        {
            f"{side}_{key}": 1.1 + offset
            for side, offset in (("bid", 0.0), ("ask", 0.00006))
            for key in ("open", "high", "low", "close")
        },
        index=idx,
    )
    j = 10
    side = "bid" if direction > 0 else "ask"
    entry = raw["ask_open" if direction > 0 else "bid_open"].iloc[j + 1]
    dist = fee_floor("EURUSD")
    opening_r = {"target_gap": 3.0, "stop_gap": -2.0, "tie": 0.0}[kind]
    opening = entry + direction * opening_r * dist
    for name in ("bid", "ask"):
        offset = 0.0 if name == side else (0.00006 if name == "ask" else -0.00006)
        raw.loc[idx[j + 2], [f"{name}_{k}" for k in ("open", "high", "low", "close")]] = [
            opening + offset,
            entry + 4 * dist + offset,
            entry - 4 * dist + offset,
            entry + offset,
        ]
    signals = pd.DataFrame(
        {"dir": 0, "stop_dist": dist, "target_dist": 2 * dist, "score": 0.9, "max_hold": 96},
        index=idx,
    )
    signals.loc[idx[j], "dir"] = direction
    return raw, signals, j


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize(
    "kind,expected_reason,expected_r",
    [("target_gap", "target_gap", 3.0), ("stop_gap", "stop_gap", -2.0), ("tie", "stop", -1.0)],
)
def test_opening_price_precedes_intrabar_extremes(direction, kind, expected_reason, expected_r):
    raw, signals, j = scenario(direction, kind)
    trades = simulate(
        {"EURUSD": raw}, {"EURUSD": signals}, raw.index[0], raw.index[-1] + BAR, PROTOCOL.base_costs
    )
    assert len(trades) == 1
    trade = trades.iloc[0]
    assert trade.reason == expected_reason
    assert trade.r_gross == pytest.approx(expected_r)
    assert trade.exit_ts == raw.index[j + 2]


@pytest.mark.parametrize(
    "kind,expected_r", [("target_gap", 3.0), ("stop_gap", -2.0), ("tie", -1.0)]
)
def test_sell_labels_follow_same_opening_chronology(kind, expected_r):
    raw, signals, j = scenario(-1, kind)
    target = labels(raw, pd.DataFrame({"atr": 0.0004}, index=raw.index), "EURUSD")
    trade = simulate(
        {"EURUSD": raw}, {"EURUSD": signals}, raw.index[0], raw.index[-1] + BAR, PROTOCOL.base_costs
    ).iloc[0]
    assert target.net_r.iloc[j] == pytest.approx(expected_r - PROTOCOL.max_fee_risk)
    assert trade.r_gross == pytest.approx(expected_r)
    assert target.net_r.iloc[j] == pytest.approx(expected_r - trade.r_cost)
    assert target.net_r.iloc[j] == pytest.approx(trade.r_net)
