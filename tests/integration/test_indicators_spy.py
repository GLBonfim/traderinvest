"""Run the full Indicator Engine on the real SPY history in the development database.

Read-only. Skipped when the database or the SPY data is unavailable.
"""

from collections.abc import Iterator

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.candles.loader import load_closed_bars
from app.core.config import Settings
from app.indicators import ENGINE_VERSION, IndicatorConfig, IndicatorEngine

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def spy() -> Iterator[tuple[int, pd.DataFrame]]:
    try:
        settings = Settings()  # type: ignore[call-arg]
        engine = create_engine(settings.database_url(), connect_args={"connect_timeout": 3})
        with Session(engine) as session:
            loaded = load_closed_bars(session, "SPY")
    except (OperationalError, LookupError) as exc:
        pytest.skip(f"SPY data unavailable: {type(exc).__name__}")
    if len(loaded[1]) < 1000:
        pytest.skip("SPY history too short for this test")
    yield loaded
    engine.dispose()


def test_full_spy_history(spy: tuple[int, pd.DataFrame]) -> None:
    instrument_id, bars = spy
    before = bars.copy(deep=True)
    a = IndicatorEngine().analyze(bars, instrument_id=instrument_id)

    pd.testing.assert_frame_equal(bars, before)  # source OHLCV untouched
    assert a.engine_version == ENGINE_VERSION
    assert a.config_fingerprint == IndicatorConfig().fingerprint()
    assert len(a.values) == len(bars)
    assert set(a.values["instrument_id"]) == {instrument_id}
    assert list(a.values.columns[:2]) == ["instrument_id", "timeframe"]
    assert len(a.catalog) == 24

    for row in a.catalog.itertuples():
        after = a.values[row.column].iloc[int(row.first_valid_index) :].to_numpy(dtype=float)
        assert not np.isnan(after).any(), f"{row.column}: NaN after warm-up"
        assert not np.isinf(after).any(), f"{row.column}: inf after warm-up"

    v = a.values
    assert v["rsi_14"].dropna().between(0, 100).all()
    assert v["stoch_k"].dropna().between(0, 100).all()
    assert (v["atr_14"].dropna() > 0).all()
    assert (v["bb_upper"].dropna() >= v["bb_lower"].dropna()).all()

    again = IndicatorEngine().analyze(bars, instrument_id=instrument_id)
    pd.testing.assert_frame_equal(a.values, again.values)
