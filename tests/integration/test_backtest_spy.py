"""All six baselines + both benchmarks on the real SPY history (read-only).

Checks temporal validity and reconciliation only. No profitability expectation is encoded.
"""

from collections.abc import Iterator

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.backtest import BENCHMARK_PRICE, BENCHMARK_TOTAL, BacktestConfig, run_suite
from app.backtest.data import load_backtest_bars
from app.core.config import Settings

pytestmark = pytest.mark.integration

STRATEGIES = ("buy_and_hold", "sma_trend", "sma_crossover", "rsi_momentum",
              "price_action_trend", "regime_trend")  # fmt: skip


@pytest.fixture(scope="module")
def spy() -> Iterator[tuple[int, pd.DataFrame]]:
    try:
        engine = create_engine(Settings().database_url(), connect_args={"connect_timeout": 3})  # type: ignore[call-arg]
        with Session(engine) as session:
            loaded = load_backtest_bars(session, "SPY")
    except (OperationalError, LookupError) as exc:
        pytest.skip(f"SPY data unavailable: {type(exc).__name__}")
    if len(loaded[1]) < 1000:
        pytest.skip("SPY history too short")
    yield loaded
    engine.dispose()


def test_full_spy_backtest(spy: tuple[int, pd.DataFrame]) -> None:
    instrument_id, bars = spy
    before = bars.copy(deep=True)
    results = run_suite(bars, config=BacktestConfig(), instrument_id=instrument_id)
    pd.testing.assert_frame_equal(bars, before)
    assert set(results) == {*STRATEGIES, BENCHMARK_PRICE, BENCHMARK_TOTAL}

    sessions = set(bars.index)
    for sid, r in results.items():
        eq, fills, trades = r.equity, r.fills, r.trades
        assert len(eq) == len(bars)
        # every fill: after its signal, at a real session open, at that session's open price
        assert (fills["ts"] > fills["signal_observed_at"]).all(), sid
        assert set(fills["ts"]) <= sessions, sid
        reference = bars["open"]
        if sid == BENCHMARK_TOTAL:  # valued with adjusted prices: open * adj_close / close
            reference = bars["open"] * bars["adj_close"] / bars["close"]
        np.testing.assert_allclose(fills["price"], reference.loc[fills["ts"]].to_numpy())
        # trades are temporally valid
        assert (trades["exit_time"] > trades["entry_time"]).all(), sid
        assert (trades["entry_time"] > trades["entry_signal_at"]).all(), sid
        # equity reconstructs; returns compound to the reported cumulative return
        np.testing.assert_allclose(
            eq["equity"], eq["cash"] + eq["shares"] * eq["close"], rtol=1e-12
        )
        assert (1 + eq["daily_return"]).prod() - 1 == pytest.approx(
            r.metrics["cumulative_return"], rel=1e-9
        )
        # costs reconcile
        open_costs = r.open_position["entry_costs"] if r.open_position else 0.0
        assert trades["total_cost"].sum() + open_costs == pytest.approx(fills["total_cost"].sum())
        assert eq["costs"].sum() == pytest.approx(r.metrics["total_costs"])
        m = r.metrics
        assert m["realized_pnl"] + m["unrealized_pnl"] == pytest.approx(m["total_pnl"])
        assert m["max_drawdown"] == pytest.approx(eq["drawdown"].min())

    # buy_and_hold strategy and the price benchmark follow the same convention
    pd.testing.assert_series_equal(
        results["buy_and_hold"].equity["equity"], results[BENCHMARK_PRICE].equity["equity"]
    )

    again = run_suite(bars, config=BacktestConfig(), instrument_id=instrument_id)
    for sid in results:
        pd.testing.assert_frame_equal(results[sid].equity, again[sid].equity)
        pd.testing.assert_frame_equal(results[sid].trades, again[sid].trades)
