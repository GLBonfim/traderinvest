"""Baseline strategies on the real SPY history in the development database (read-only).

State counts are a REGRESSION snapshot of engine 1.0.0 with default configuration — states only,
no returns or performance (Phase 8).
"""

from collections.abc import Iterator

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.candles.loader import load_closed_bars
from app.core.config import Settings
from app.strategies import ENGINE_VERSION, StrategyEngine

pytestmark = pytest.mark.integration

# strategy_id: (n_long, n_flat, n_insufficient_data, state_changes, first_decision_bar)
EXPECTED = {
    "buy_and_hold": (8479, 0, 0, 0, 0),
    "sma_trend": (6209, 2071, 199, 223, 199),
    "sma_crossover": (5606, 2824, 49, 179, 49),
    "rsi_momentum": (5603, 2862, 14, 948, 14),
    "price_action_trend": (1647, 6781, 51, 121, 51),
    "regime_trend": (1639, 6568, 272, 117, 272),
}


@pytest.fixture(scope="module")
def spy() -> Iterator[tuple[int, pd.DataFrame]]:
    try:
        engine = create_engine(Settings().database_url(), connect_args={"connect_timeout": 3})  # type: ignore[call-arg]
        with Session(engine) as session:
            loaded = load_closed_bars(session, "SPY")
    except (OperationalError, LookupError) as exc:
        pytest.skip(f"SPY data unavailable: {type(exc).__name__}")
    if len(loaded[1]) != 8479:
        pytest.skip("snapshot recorded on the 8,479-bar SPY history")
    yield loaded
    engine.dispose()


def test_full_spy_history(spy: tuple[int, pd.DataFrame]) -> None:
    instrument_id, bars = spy
    before = bars.copy(deep=True)
    run = StrategyEngine().run(bars, instrument_id=instrument_id)
    pd.testing.assert_frame_equal(bars, before)
    assert run.engine_version == ENGINE_VERSION

    got = {
        r.strategy_id: (
            r.n_long,
            r.n_flat,
            r.n_insufficient_data,
            r.state_changes,
            r.first_decision_bar,
        )
        for r in run.summary.itertuples()
    }
    assert got == EXPECTED

    s = run.signals
    assert len(s) == 6 * len(bars)
    assert (s["observed_at"] > s["bar_ts"]).all()
    assert (s["effective_at"] > s["observed_at"]).all()
    for _, g in s.groupby("strategy_id"):
        assert g["state_in_effect"].iloc[1:].tolist() == g["state"].iloc[:-1].tolist()
        # effective_at of bar T is exactly the session of bar T+1 (no missing sessions in SPY)
        assert (g["effective_at"].iloc[:-1].to_numpy() == g["bar_ts"].iloc[1:].to_numpy()).all()

    again = StrategyEngine().run(bars, instrument_id=instrument_id)
    pd.testing.assert_frame_equal(run.signals, again.signals)
