import pandas as pd
import pytest

from app.fx.research.backtest import EUR_TO
from app.fx.research.m5_daily_diagnostics import daily_realized


def test_same_day_loss_and_gain_net_after_costs():
    trades = pd.DataFrame(
        {
            "pair": ["EURUSD", "EURUSD", "EURUSD"],
            "exit_ts": ["2024-01-01T12:00:00Z", "2024-01-01T16:00:00Z", "2024-01-02T12:00:00Z"],
            "r_net": [1.0, -2.0, 3.0],
            "units": [1000.0] * 3,
            "stop_dist": [0.001] * 3,
        }
    )
    out = daily_realized(trades)
    assert out["positive_days"] == 1 and out["negative_days"] == 1
    assert out["daily"][0]["net_eur_estimated"] == pytest.approx(-1 / EUR_TO["USD"])
    assert not out["includes_unrealized"] and not out["approved"]
