"""Bar backtester: bid/ask fills, conservative intrabar order, gaps, time and weekend exits,
costs in R. Hand-built bars, no data files."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.fx.research.backtest import CostModel, run_backtest, summarize

T0 = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
FREE = CostModel(commission_bps=0.0, commission_min_usd=0.0, financing_bps_per_night=0.0)
SPREAD = 0.0002


def bars(
    mids: list[tuple[float, float, float, float]],
    start: datetime = T0,
    step: timedelta = timedelta(minutes=15),
    gaps: dict[int, timedelta] | None = None,
) -> pd.DataFrame:
    idx, t = [], start
    for i in range(len(mids)):
        if gaps and i in gaps:
            t += gaps[i]
        idx.append(t)
        t += step
    rows = {}
    for side, sign in (("bid", -1), ("ask", 1)):
        for k, j in (("open", 0), ("high", 1), ("low", 2), ("close", 3)):
            rows[f"{side}_{k}"] = [m[j] + sign * SPREAD / 2 for m in mids]
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def signals(
    df: pd.DataFrame, at: int, d: int, stop: float, target: float, hold: int = 50
) -> pd.DataFrame:
    s = pd.DataFrame(
        {"dir": 0, "stop_dist": np.nan, "target_dist": np.nan, "max_hold": 0}, index=df.index
    )
    s.iloc[at] = [d, stop, target, hold]
    return s


FLAT = (1.1000, 1.1002, 1.0998, 1.1000)


def test_long_target_fills_at_the_ask_and_exits_at_the_target() -> None:
    df = bars([FLAT, FLAT, (1.1000, 1.1035, 1.0995, 1.1030), FLAT])
    t = run_backtest(df, signals(df, 0, 1, 0.0020, 0.0030), "EURUSD", costs=FREE)
    assert len(t) == 1
    tr = t.iloc[0]
    assert tr["entry"] == pytest.approx(1.1000 + SPREAD / 2)  # next bar open, ask side
    assert tr["reason"] == "target" and tr["r_gross"] == pytest.approx(1.5)
    assert tr["entry_ts"] == df.index[1]


def test_stop_assumed_first_when_both_inside_one_bar() -> None:
    df = bars([FLAT, FLAT, (1.1000, 1.1050, 1.0950, 1.1000)])
    t = run_backtest(df, signals(df, 0, 1, 0.0020, 0.0030), "EURUSD", costs=FREE)
    assert t.iloc[0]["reason"] == "stop" and t.iloc[0]["r_gross"] == pytest.approx(-1.0)


def test_gap_through_the_stop_fills_at_the_open() -> None:
    df = bars([FLAT, FLAT, (1.0960, 1.0965, 1.0955, 1.0960)])
    t = run_backtest(df, signals(df, 0, 1, 0.0020, 0.0030), "EURUSD", costs=FREE)
    tr = t.iloc[0]
    assert tr["reason"] == "stop_gap" and tr["r_gross"] < -1.0


def test_short_uses_the_ask_to_exit() -> None:
    df = bars([FLAT, FLAT, (1.1000, 1.1001, 1.0965, 1.0970)])
    t = run_backtest(df, signals(df, 0, -1, 0.0020, 0.0030), "EURUSD", costs=FREE)
    tr = t.iloc[0]
    assert tr["entry"] == pytest.approx(1.1000 - SPREAD / 2)  # sells at the bid
    assert tr["reason"] == "target" and tr["r_gross"] == pytest.approx(1.5)


def test_time_stop_and_weekend_exit() -> None:
    df = bars([FLAT] * 6)
    t = run_backtest(df, signals(df, 0, 1, 0.0020, 0.0030, hold=2), "EURUSD", costs=FREE)
    assert t.iloc[0]["reason"] == "time" and t.iloc[0]["bars"] == 2
    wk = bars([FLAT] * 6, gaps={3: timedelta(days=2)})
    t = run_backtest(wk, signals(wk, 0, 1, 0.0020, 0.0030), "EURUSD", costs=FREE)
    assert t.iloc[0]["reason"] == "weekend" and t.iloc[0]["exit_ts"] == wk.index[2]


def test_no_entry_when_not_allowed_or_across_a_gap() -> None:
    df = bars([FLAT] * 4)
    sig = signals(df, 0, 1, 0.0020, 0.0030)
    allowed = pd.Series(False, index=df.index)
    assert run_backtest(df, sig, "EURUSD", allowed, FREE).empty
    gap = bars([FLAT] * 4, gaps={1: timedelta(days=2)})
    assert run_backtest(gap, signals(gap, 0, 1, 0.002, 0.003), "EURUSD", costs=FREE).empty


def test_costs_in_r_commission_minimum_and_financing() -> None:
    start = datetime(2026, 10, 14, 20, 30, tzinfo=UTC)  # crosses 17:00 New York (21:00 UTC)
    df = bars([FLAT] * 6, start=start)
    costs = CostModel(
        commission_bps=0.2,
        commission_min_usd=2.0,
        financing_bps_per_night=0.5,
        account_equity=10_000,
        risk_pct=0.5,
    )
    t = run_backtest(df, signals(df, 0, 1, 0.0020, 0.0030, hold=4), "EURUSD", costs=costs)
    tr = t.iloc[0]
    assert tr["nights"] == 1
    risk_q = 10_000 * 0.005 * 1.10  # 55 USD at risk
    units = risk_q / 0.0020
    comm = 2 * max(2.0, 0.2e-4 * units * tr["entry"])
    fin = 0.5e-4 * units * tr["entry"]
    assert tr["r_cost"] == pytest.approx((comm + fin) / risk_q)
    assert tr["r_net"] == pytest.approx(tr["r_gross"] - tr["r_cost"])


def test_one_position_at_a_time_and_summary() -> None:
    df = bars([FLAT] * 10)
    sig = pd.DataFrame(
        {"dir": 1, "stop_dist": 0.002, "target_dist": 0.003, "max_hold": 3}, index=df.index
    )
    t = run_backtest(df, sig, "EURUSD", costs=FREE)
    assert (t["entry_ts"].iloc[1:].to_numpy() > t["exit_ts"].iloc[:-1].to_numpy()).all()
    s = summarize(t, days=1)
    assert s["trades"] == len(t) and s["trades_per_day"] == len(t)
    assert summarize(t.iloc[:0])["trades"] == 0


@pytest.mark.parametrize(
    ("r", "dd"),
    [([-1.0], 1.0), ([-1.0, -1.0], 2.0), ([2.0, -1.0, -1.0, 3.0], 2.0), ([1.0, 1.0], 0.0)],
)
def test_max_drawdown_counts_from_the_starting_capital(r: list[float], dd: float) -> None:
    trades = pd.DataFrame({"r_net": r, "r_cost": 0.0, "bars": 1})
    assert summarize(trades)["max_dd_r"] == pytest.approx(dd)
