"""Full Phase 9 validation on the real SPY history with the DEFAULT configuration
(2,000 resamples, block 21, 95%, seed 20261007; 3 slices; 4 cost scenarios). Read-only.

Checks structure, reconciliation with Phase 8 and determinism. No outcome is asserted.
"""

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
from app.strategies import StrategyEngine
from app.validation import ValidationConfig, Validator

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def spy() -> Iterator[pd.DataFrame]:
    try:
        engine = create_engine(Settings().database_url(), connect_args={"connect_timeout": 3})  # type: ignore[call-arg]
        with Session(engine) as session:
            _, bars = load_backtest_bars(session, "SPY")
    except (OperationalError, LookupError) as exc:
        pytest.skip(f"SPY data unavailable: {type(exc).__name__}")
    if len(bars) < 5000:
        pytest.skip("SPY history too short")
    yield bars
    engine.dispose()


def test_full_spy_validation(spy: pd.DataFrame) -> None:
    before = spy.copy(deep=True)
    cfg = ValidationConfig()
    report = Validator(cfg).run(spy)
    pd.testing.assert_frame_equal(spy, before)
    f = report.frames()

    # formal family: exactly the declared 30 tests on the full slice, all defined on SPY
    comps = f["comparisons"]
    formal = comps[comps["family"] == "formal"]
    assert len(formal) == 30 and set(formal["prov_slice_name"]) == {"full"}
    assert formal["p_value"].notna().all() and (formal["p_holm"] >= formal["p_value"]).all()
    assert set(f["intervals"]["prov_n_resamples"]) == {2000}
    assert set(f["intervals"]["prov_block_length"]) == {21}

    # Phase 8 reconciliation (full slice, default costs)
    bars = spy
    run = StrategyEngine().run(bars.drop(columns=["adj_close"]))
    bt = Backtester(BacktestConfig(period_label="full"))
    res = {**bt.run(bars, run), **bt.benchmarks(bars)}
    iv = f["intervals"][f["intervals"]["prov_slice_name"] == "full"].set_index(
        ["subject", "metric"]
    )
    for sid, r in res.items():
        for m in ("cumulative_return", "cagr", "sharpe", "max_drawdown"):
            assert iv.loc[(sid, m), "point"] == pytest.approx(r.metrics[m], rel=1e-9)

    # cost sensitivity: 4 scenarios x 8 subjects x 3 slices; costs never change orders
    cs = f["cost_sensitivity"]
    assert len(cs) == 4 * 8 * 3
    orders = cs.pivot_table(index=["slice_name", "subject"], columns="scenario", values="orders")
    assert (orders.nunique(axis=1) == 1).all()
    assert (cs[cs["scenario"] == "A_zero"]["total_costs"] == 0).all()

    # redundancy of the two trend baselines is reported for every slice
    red = f["redundancy"]
    assert set(red["slice_name"]) == {"full", "early", "late"}
    assert red["position_agreement"].between(0, 1).all()

    # determinism: re-running the full-slice bootstrap reproduces the report exactly
    returns = pd.DataFrame({sid: r.equity["daily_return"] for sid, r in res.items()})
    for sid in ("buy_and_hold", "sma_trend", "sma_crossover", "rsi_momentum",
                "price_action_trend", "regime_trend"):  # fmt: skip
        returns[f"{sid}__gross"] = res[sid].equity["gross_daily_return"]
    intervals, _ = Validator(cfg).analyze_returns(
        returns,
        slice_name="full",
        cost_scenario="D_default",
        backtest_fingerprint=bt.config.fingerprint(),
    )
    again = {(i.subject, i.metric): (i.lower, i.upper) for i in intervals}
    for (subject, metric), row in iv.iterrows():
        assert again[(subject, metric)] == pytest.approx((row["lower"], row["upper"])) or (
            np.isnan(row["lower"]) and np.isnan(again[(subject, metric)][0])
        )
