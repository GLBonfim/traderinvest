"""Run the Regime Engine on the real SPY history in the development database (read-only).

The label counts are a REGRESSION snapshot of engine 1.0.0 with default configuration. They are
descriptive and were not used to choose any threshold.
"""

from collections.abc import Iterator

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.candles.loader import load_closed_bars
from app.core.config import Settings
from app.regimes import ENGINE_VERSION, RegimeEngine
from app.regimes.engine import DIMENSIONS

pytestmark = pytest.mark.integration

EXPECTED_COUNTS = {
    "trend_regime": {"transition": 6186, "uptrend": 1647, "downtrend": 409, "range": 186,
                     "insufficient_data": 51},
    "volatility_regime": {"normal": 4282, "low": 1903, "high": 1408, "extreme": 614,
                          "insufficient_data": 272},
    "composite_regime": {"transition": 3102, "trending_up": 1639,
                         "high_volatility_transition": 1629, "low_volatility_transition": 1263,
                         "trending_down": 388, "insufficient_data": 272, "ranging": 186},
}  # fmt: skip


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
    a = RegimeEngine().analyze(bars, instrument_id=instrument_id)
    st = a.state

    pd.testing.assert_frame_equal(bars, before)
    assert a.engine_version == ENGINE_VERSION
    assert len(st) == len(bars) and set(st["instrument_id"]) == {instrument_id}
    for col, expected in EXPECTED_COUNTS.items():
        assert st[col].value_counts().to_dict() == expected, col
    first_vol = st.index.get_loc(st.index[st["volatility_regime"] != "insufficient_data"][0])
    assert first_vol == 272
    for dim in DIMENSIONS:
        assert st[f"{dim}_regime"].notna().all()
    # Regression anchor: 2020-03-16 (largest COVID-crash volatility) ranks at the top of its past.
    day = st.loc["2020-03-16"].iloc[0]
    assert day["volatility_regime"] == "extreme" and day["volatility_percentile"] == 1.0

    again = RegimeEngine().analyze(bars, instrument_id=instrument_id)
    pd.testing.assert_frame_equal(st, again.state)
