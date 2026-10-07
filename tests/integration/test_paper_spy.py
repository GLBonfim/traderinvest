"""Phase 12 on the real SPY history: replay invariants for all six baselines, control vs
Phase 8, rebalancing scenario, determinism, incremental == replay with restarts, idempotency.
No outcome (profitability) is asserted."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.backtest import BacktestConfig, Backtester
from app.backtest.data import load_backtest_bars
from app.core.config import Settings
from app.paper import PaperConfig, PaperStore, baseline_inputs, replay_baselines
from app.paper.engine import PaperRun, PaperTradingEngine
from app.paper.ledger import reconstruct_snapshots
from app.paper.models import FILLED
from app.paper.state import to_dict
from app.risk import SCENARIOS
from app.strategies import StrategyEngine

pytestmark = pytest.mark.integration
OHLCV = ["open", "high", "low", "close", "volume"]
VOL = PaperConfig(risk=next(s for s in SCENARIOS if s.name == "volatility_target_10"))


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


@pytest.fixture(scope="module")
def inputs(spy: pd.DataFrame):  # type: ignore[no-untyped-def]
    return baseline_inputs(spy)


def check_invariants(r: PaperRun, bars: pd.DataFrame, initial: float) -> None:
    d, o, f, p = r.decisions, r.orders, r.fills, r.portfolio
    sessions = pd.DatetimeIndex(bars.index)
    # timestamps: every decision at a real session close, every fill at a real session open
    assert d["bar_ts"].isin(sessions).all() and (d["observed_at"] > d["bar_ts"]).all()
    assert (d["scheduled_execution_at"] > d["observed_at"]).all()
    assert f["executed_at"].isin(sessions).all()
    np.testing.assert_array_equal(
        f["price"].to_numpy(), bars.loc[pd.DatetimeIndex(f["executed_at"]), "open"].to_numpy()
    )
    # decision -> risk -> instruction -> order -> fill ordering
    j = f.merge(o, on="order_id").merge(d, on="decision_id")
    assert len(j) == len(f) and (j["status"] == FILLED).all()
    assert (j["executed_at"] == j["scheduled_execution_at_x"]).all()
    assert (j["executed_at"] > j["observed_at"]).all()
    nxt = pd.Series(sessions[1:], index=sessions[:-1])
    assert (j["executed_at"].to_numpy() == nxt.loc[pd.DatetimeIndex(j["bar_ts"])].to_numpy()).all()
    # no impossible positions
    assert (p["position_quantity"] >= 0).all() and (p["cash"] >= 0).all()
    assert (p["gross_exposure"] <= 1).all()
    # accounting reconciliation
    np.testing.assert_array_equal(p["equity"], p["cash"] + p["position_quantity"] * p["mark_price"])
    np.testing.assert_array_equal(p["total_pnl"], p["realized_pnl"] + p["unrealized_pnl"])
    np.testing.assert_allclose(p["total_pnl"], p["equity"] - initial, rtol=0, atol=1e-6)
    assert p["cumulative_costs"].iloc[-1] == pytest.approx(f["total_cost"].sum(), abs=1e-6)
    assert f["realized_pnl"].sum() == pytest.approx(p["realized_pnl"].iloc[-1], abs=1e-6)
    # snapshots are reproducible from the ledger alone
    rebuilt = pd.DataFrame(reconstruct_snapshots(r.events, initial))
    for col in rebuilt.columns:
        np.testing.assert_array_equal(rebuilt[col].to_numpy(), p[col].to_numpy())


def test_spy_replay_all_baselines(spy: pd.DataFrame) -> None:
    before = spy.copy(deep=True)
    runs = replay_baselines(spy, PaperConfig())
    pd.testing.assert_frame_equal(spy, before)
    assert len(runs) == 6
    p8 = Backtester(BacktestConfig()).run(spy, StrategyEngine().run(spy[OHLCV]))
    initial = BacktestConfig().initial_capital
    for sid, r in runs.items():
        check_invariants(r, spy, initial)
        ref = p8[sid]
        assert len(r.fills) == len(ref.fills)
        np.testing.assert_array_equal(r.fills["executed_at"].to_numpy(), ref.fills["ts"].to_numpy())
        np.testing.assert_allclose(r.equity["equity"], ref.equity["equity"], rtol=0, atol=1e-6)

    again = replay_baselines(spy, PaperConfig())
    for sid, r in runs.items():
        assert again[sid].events == r.events  # deterministic, including every ID and hash


def test_spy_rebalancing_scenario(spy: pd.DataFrame) -> None:
    runs = replay_baselines(spy, VOL)
    actions: set[str] = set()
    for r in runs.values():
        check_invariants(r, spy, VOL.backtest.initial_capital)
        actions |= set(r.fills["action"])
        assert set(r.decisions["pending_kind"]) <= {"none", "entry", "exit", "rebalance"}
    assert actions == {"entry", "add", "reduce", "exit"}


@pytest.mark.parametrize("sid", ["rsi_momentum", "sma_trend"])
def test_spy_incremental_with_restarts_equals_replay(inputs, tmp_path: Path, sid: str) -> None:  # type: ignore[no-untyped-def]
    version, bars_in = inputs[sid]
    ref = PaperTradingEngine(VOL).replay_inputs(bars_in, strategy_id=sid, strategy_version=version)

    def open_store() -> PaperStore:
        return PaperStore(VOL, strategy_id=sid, strategy_version=version, root=tmp_path)

    def ny(b) -> date:  # type: ignore[no-untyped-def]
        return b.bar_ts.tz_convert("America/New_York").date()

    s = open_store()
    first = [b for b in bars_in if ny(b) <= date(2020, 2, 14)]
    assert s.process_many(first)["processed"] == len(first)
    # selected interval (COVID crash): restart before every session
    window = [b for b in bars_in if date(2020, 2, 14) < ny(b) <= date(2020, 4, 30)]
    for b in window:
        s = open_store()
        assert s.process(b).status == "processed"
        assert s.process(b).status == "already_processed"  # same bar twice: no effect
    s = open_store()
    counts = s.process_many(bars_in)  # the whole history again: only new sessions processed
    assert counts["already_processed"] == len(first) + len(window)
    assert counts["processed"] == len(bars_in) - len(first) - len(window)

    final = open_store()
    assert final.events() == ref.events
    assert to_dict(final.trader.state) == to_dict(ref.final_state)
