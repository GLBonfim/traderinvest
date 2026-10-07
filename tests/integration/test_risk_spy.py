"""Phase 11 on the real SPY history: control == Phase 8, invariants for every overlay,
determinism. No outcome is asserted."""

from collections.abc import Iterator

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.backtest import BacktestConfig, Backtester
from app.backtest.data import load_backtest_bars
from app.core.config import Settings
from app.risk import SCENARIOS, run_risk_suite
from app.strategies import StrategyEngine
from app.validation.config import SliceSpec

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def spy() -> Iterator[pd.DataFrame]:
    try:
        engine = create_engine(Settings().database_url(), connect_args={"connect_timeout": 3})  # type: ignore[call-arg]
        with Session(engine) as session:
            _, bars = load_backtest_bars(session, "SPY")
    except (OperationalError, LookupError) as exc:
        pytest.skip(f"SPY data unavailable: {type(exc).__name__}")
    if len(bars) < 8000:
        pytest.skip("full SPY history required")
    yield bars
    engine.dispose()


def test_full_spy_risk_suite(spy: pd.DataFrame) -> None:
    before = spy.copy(deep=True)
    report = run_risk_suite(spy)
    pd.testing.assert_frame_equal(spy, before)
    assert len(report.runs) == 3 * 6 * len(SCENARIOS)
    assert len(report.comparisons) == 3 * 6 * (len(SCENARIOS) - 1) * 4

    p8 = Backtester(BacktestConfig()).run(
        spy, StrategyEngine().run(spy[["open", "high", "low", "close", "volume"]])
    )
    for sid, res in p8.items():
        ctl = report.runs[("full", sid, "control_no_overlay")]
        np.testing.assert_array_equal(
            ctl.equity["equity"].to_numpy(), res.equity["equity"].to_numpy()
        )

    for (_, _, _), r in report.runs.items():
        assert (r.equity["position"] <= 1 + 1e-9).all() and (r.equity["cash"] >= -1e-6).all()
        if len(r.fills):
            assert (r.fills["ts"] > r.fills["signal_observed_at"]).all()
        assert set(r.decisions["requested_exposure"]) <= {0.0, 1.0}
        assert (r.decisions["approved_exposure"] <= r.decisions["requested_exposure"] + 1e-12).all()

    again = run_risk_suite(spy, slices=(SliceSpec("full"),), compare=False)
    for key, r in again.runs.items():
        pd.testing.assert_frame_equal(r.equity, report.runs[key].equity)
        pd.testing.assert_frame_equal(r.decisions, report.runs[key].decisions)
