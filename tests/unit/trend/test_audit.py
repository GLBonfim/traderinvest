import numpy as np
import pandas as pd
import pytest

from app.trend import audit as qa
from app.trend.loaders import total_return_factors
from app.trend.sources import yahoo_frame_to_csv


def _frame(
    dates: pd.DatetimeIndex, close: list[float], div: list[float] | None = None
) -> pd.DataFrame:
    c = pd.Series(close, index=dates)
    d = pd.Series(div or [0.0] * len(close), index=dates)
    # provider method: adjusted return = close_t / (close_{t-1} - dividend_t) - 1
    adj = (c / (c.shift(1) - d)).fillna(1.0).cumprod()
    return pd.DataFrame(
        {
            "open": c,
            "high": c,
            "low": c,
            "close": c,
            "adj_close": adj,
            "volume": 100.0,
            "dividends": d,
            "splits": 0.0,
        }
    )


def test_total_return_factor_books_dividend_at_ex_date() -> None:
    idx = pd.date_range("2024-01-01", periods=3, freq="B")
    f = total_return_factors(pd.Series([100.0, 98.0, 99.0], idx), pd.Series([0.0, 3.0, 0.0], idx))
    assert np.isnan(f.iloc[0])
    assert f.iloc[1] == pytest.approx(1.01)  # (98 + 3) / 100
    assert f.iloc[2] == pytest.approx(99.0 / 98.0)


def test_longest_constant_run() -> None:
    s = pd.Series([1, 1, 2, 2, 2, 3], index=pd.date_range("2024-01-01", periods=6))
    assert qa.longest_constant_run(s) == (3, "2024-01-05")


def test_audit_prices_detects_missing_session_and_bad_adjustment() -> None:
    sessions = qa.calendar_sessions("XNYS", pd.Timestamp("2024-07-01"), pd.Timestamp("2024-07-12"))
    kept = sessions.delete(3)  # drop one real session
    frame = _frame(kept, [100.0 + i for i in range(len(kept))])
    rep = qa.audit_prices(frame, "XNYS")
    assert rep["missing_sessions"] == 1
    assert rep["missing_sessions_sample"] == [sessions[3].date().isoformat()]
    assert rep["adjustment_sessions_above_tolerance"] == 0
    frame.loc[frame.index[4], "dividends"] = 2.0  # a dividend the provider never adjusted for
    assert qa.audit_prices(frame, "XNYS")["adjustment_sessions_above_tolerance"] == 1


def test_audit_prices_lists_extreme_moves() -> None:
    idx = qa.calendar_sessions("XNYS", pd.Timestamp("2024-07-01"), pd.Timestamp("2024-07-10"))
    close = [100.0] * len(idx)
    close[-1] = 80.0
    rep = qa.audit_prices(_frame(idx, close), "XNYS")
    assert [m["date"] for m in rep["extreme_moves"]] == [idx[-1].date().isoformat()]


def test_fx_staleness_counts_weekdays_since_latest_rate() -> None:
    fx = pd.Series([1.1, 1.2], index=pd.to_datetime(["2024-03-28", "2024-04-02"]))
    dates = pd.DatetimeIndex(["2024-03-27", "2024-03-28", "2024-04-01", "2024-04-02"])
    out = qa.fx_staleness(fx, dates)
    assert out.tolist() == [-1, 0, 2, 0]  # no prior rate; same day; Fri + Mon gap; same day


def test_evaluable_window_uses_the_200th_session_of_the_latest_asset() -> None:
    a = pd.bdate_range("2020-01-01", periods=400)
    b = pd.bdate_range("2020-03-02", periods=300)
    master = a.union(b)
    w = qa.evaluable_window({"A": a, "B": b}, {}, master, pd.Timestamp(master[-1]), warmup=200)
    assert w.binding_asset == "B"
    assert w.evaluable_start == b[199].date().isoformat()


def test_evaluable_window_waits_for_benchmark_only_series() -> None:
    a = pd.bdate_range("2020-01-01", periods=400)
    late = pd.Timestamp(a[300])
    w = qa.evaluable_window({"A": a}, {"BENCH": late}, a, pd.Timestamp(a[-1]), warmup=200)
    assert w.binding_asset == "BENCH"
    assert w.evaluable_start == late.date().isoformat()


def test_evaluable_window_refuses_short_series() -> None:
    a = pd.bdate_range("2020-01-01", periods=150)
    with pytest.raises(ValueError, match="needs 200"):
        qa.evaluable_window({"A": a}, {}, a, pd.Timestamp(a[-1]))


def test_partitions_cut_at_month_ends_and_do_not_overlap() -> None:
    master = pd.bdate_range("2010-01-01", "2024-12-31")
    p = qa.partition_bounds(master, pd.Timestamp(master[0]), pd.Timestamp(master[-1]))
    dev_end = pd.Timestamp(p["development"]["end"])
    val_end = pd.Timestamp(p["validation"]["end"])
    for cut in (dev_end, val_end):
        assert master[master > cut][0].month != cut.month  # last session of its month
    assert pd.Timestamp(p["validation"]["start"]) == master[master > dev_end][0]
    assert pd.Timestamp(p["sealed"]["start"]) == master[master > val_end][0]
    n = len(master)
    # A month-end cut lands at most one month (< 24 sessions) before the exact fraction.
    assert master.get_loc(dev_end) < 0.60 * n <= master.get_loc(dev_end) + 24
    assert master.get_loc(val_end) < 0.80 * n <= master.get_loc(val_end) + 24


def test_yahoo_csv_uses_local_session_dates() -> None:
    idx = pd.DatetimeIndex(["2024-07-01 00:00", "2024-07-02 00:00"]).tz_localize("Europe/Berlin")
    frame = pd.DataFrame(
        {
            "Open": 1.0,
            "High": 1.0,
            "Low": 1.0,
            "Close": [1.0, 2.0],
            "Adj Close": [1.0, 2.0],
            "Volume": 5,
            "Dividends": 0.0,
            "Stock Splits": 0.0,
        },
        index=idx,
    )
    lines = yahoo_frame_to_csv(frame).splitlines()
    assert lines[0] == "date,open,high,low,close,adj_close,volume,dividends,splits"
    assert lines[1].startswith("2024-07-01,")
