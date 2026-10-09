"""Phase 13 on the real SPY dataset: every dashboard number agrees with its domain engine
(Phases 2-12), and every page renders without an exception."""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.backtest.data import load_backtest_bars
from app.backtest.engine import run_suite, session_times
from app.core.config import Settings
from app.dashboard.services import analysis, market, paper, research
from app.data.calendar import TradingCalendar
from app.ml.engine import MLExperiment
from app.paper.engine import PaperTradingEngine, baseline_inputs
from app.risk.config import SCENARIOS
from app.risk.engine import RiskOverlay
from app.strategies.engine import StrategyEngine
from app.validation.engine import Validator

pytestmark = pytest.mark.integration
OHLCV = ["open", "high", "low", "close", "volume"]


@pytest.fixture(scope="module")
def data() -> Iterator[tuple[market.DatasetIdentity, pd.DataFrame]]:
    try:
        engine = create_engine(Settings().database_url(), connect_args={"connect_timeout": 3})  # type: ignore[call-arg]
        with Session(engine) as s:
            ident = market.dataset_identity(s, "SPY")
            bars = market.load_bars(s, ident)
    except (OperationalError, LookupError) as exc:
        pytest.skip(f"SPY data unavailable: {type(exc).__name__}")
    if len(bars) < 8000:
        pytest.skip("full SPY history required")
    yield ident, bars
    engine.dispose()


@pytest.fixture(scope="module")
def bundle(data) -> analysis.EngineBundle:  # type: ignore[no-untyped-def]
    return analysis.compute_engines(data[1])


def test_market_overview_and_identity(data) -> None:  # type: ignore[no-untyped-def]
    ident, bars = data
    engine = create_engine(Settings().database_url())  # type: ignore[call-arg]
    with Session(engine) as s:
        _, ref = load_backtest_bars(s, "SPY")
        ing = market.latest_ingestion(s, ident.instrument_id)
    pd.testing.assert_frame_equal(bars, ref)
    ov = market.market_overview(bars)
    assert ov["close"] == ref["close"].iloc[-1] and ov["previous_close"] == ref["close"].iloc[-2]
    assert ov["bars"] == ident.bars == len(ref)
    assert ident.last_ts == ref.index[-1] and ident.first_ts == ref.index[0]
    assert ing is not None and ing["status"] == "succeeded"


@pytest.mark.parametrize("session", [date(2020, 3, 16), None])
def test_engine_views_agree_with_engines(data, bundle, session) -> None:  # type: ignore[no-untyped-def]
    _, bars = data
    ts = bars.index[-1] if session is None else market.bar_ts_for(bars, session)
    ohlcv = bars[OHLCV]
    cv = analysis.candle_view(bundle, ts)
    obs = bundle.candles.observations
    assert [p["pattern"] for p in cv["patterns"]] == obs.loc[obs["ts"] == ts, "pattern"].tolist()
    pa = analysis.price_action_view(bundle, ts)
    assert pa["state"]["structure"] == bundle.price_action.state.loc[ts, "structure"]
    assert (analysis.swings_known_at(bundle, ts)["confirmed_at"] <= ts).all()
    iv = analysis.indicator_view(bundle, ts).set_index("indicator")
    np.testing.assert_array_equal(
        iv["value"].to_numpy(float),
        bundle.indicators.values.loc[ts, iv.index.tolist()].to_numpy(float),
    )
    rv = analysis.regime_view(bundle, ts)
    assert (
        rv["dimensions"]["composite"]["label"] == bundle.regimes.state.loc[ts, "composite_regime"]
    )
    st = analysis.strategy_states(bundle, ts).set_index("strategy_id")
    sig = StrategyEngine().run(ohlcv).signals
    for sid in analysis.STRATEGIES:
        row = sig[(sig["strategy_id"] == sid) & (sig["bar_ts"] == ts)].iloc[0]
        assert st.loc[sid, "state"] == row["state"] and st.loc[sid, "reason"] == row["reason"]


def test_backtest_numbers_agree_with_phase8(data) -> None:  # type: ignore[no-untyped-def]
    _, bars = data
    out = research.backtest_suite(bars)
    ref = run_suite(bars)
    assert set(out["table"].index) == set(ref)
    for sid, r in ref.items():
        for col in research.BACKTEST_COLUMNS:
            a, b = out["table"].loc[sid, col], r.metrics[col]
            assert (a == b) or (pd.isna(a) and pd.isna(b)), (sid, col)
        pd.testing.assert_series_equal(
            research.equity_curves(out["results"])[sid],
            r.equity["cumulative_return"],
            check_names=False,
        )


def test_validation_results_agree_with_phase9(data, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ident, bars = data
    research.compute_validation(bars, tmp_path, ident.key)
    frames, _ = research.load_validation(tmp_path, ident.key)
    ref = Validator().run(bars).frames()
    for name in ("comparisons", "intervals", "cost_sensitivity"):
        num = ref[name].select_dtypes("number").columns
        np.testing.assert_allclose(
            frames[name][num].to_numpy(float),
            ref[name][num].to_numpy(float),
            rtol=1e-12,
            equal_nan=True,
        )
    assert research.validation_conclusion(frames["comparisons"]) == "NO STATISTICAL EDGE FOUND"


def test_ml_results_agree_with_phase10(data, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ident, bars = data
    research.compute_ml(bars, tmp_path, ident.instrument_id)
    frames, meta = research.load_ml(tmp_path, research.ml_experiment_id(bars))
    report = MLExperiment().run(bars, instrument_id=ident.instrument_id)
    assert meta["experiment_id"] == report.experiment_id
    assert meta["overall_verdict"] == report.overall_verdict == "NO INCREMENTAL EDGE FOUND"
    ref = pd.DataFrame(report.classification)
    np.testing.assert_allclose(frames["classification"]["roc_auc"], ref["roc_auc"], rtol=1e-12)
    assert frames["verdicts"]["verdict"].tolist() == [v["verdict"] for v in report.verdicts]
    p = frames["predictions"]
    assert set(p["period"].dropna()) >= {"train", "validation", "test"}
    assert (
        (p["p_logistic_regression"].dropna() >= 0) & (p["p_logistic_regression"].dropna() <= 1)
    ).all()


@pytest.mark.parametrize("scenario", ["control_no_overlay", "volatility_target_10"])
def test_risk_outputs_agree_with_phase11(data, bundle, scenario: str) -> None:  # type: ignore[no-untyped-def]
    _, bars = data
    run = research.risk_run(bars, bundle, "sma_trend", scenario)
    times = session_times(pd.DatetimeIndex(bars.index), TradingCalendar("XNYS"))
    sig = StrategyEngine().run(bars[OHLCV]).signals
    g = sig[sig["strategy_id"] == "sma_trend"]
    st = pd.Series(g["state"].to_numpy(), index=pd.DatetimeIndex(g["bar_ts"]))
    ref = RiskOverlay(next(s for s in SCENARIOS if s.name == scenario)).run(
        bars, st, bundle.indicators.values, times, strategy_id="sma_trend"
    )
    pd.testing.assert_frame_equal(run.equity, ref.equity)
    pd.testing.assert_frame_equal(run.decisions, ref.decisions)
    d = research.risk_decision_at(run, bars.index[-1])
    assert d["approved_exposure"] == ref.decisions["approved_exposure"].iloc[-1]
    assert d["requested_exposure"] == ref.decisions["requested_exposure"].iloc[-1]


def test_paper_account_agrees_with_phase12(data, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    _, bars = data
    version, inputs = baseline_inputs(bars)["sma_trend"]
    start = date(2024, 1, 2)
    store = paper.paper_create_account(
        tmp_path,
        strategy_id="sma_trend",
        strategy_version=version,
        scenario_name="volatility_target_10",
        instrument="SPY",
    )
    paper.paper_process_sessions(store, inputs, start)
    (ref_acct,) = paper.list_accounts(tmp_path)
    view = paper.account_view(paper.open_account(ref_acct, tmp_path))
    sub = [b for b in inputs if b.bar_ts.tz_convert("America/New_York").date() >= start]
    run = PaperTradingEngine(paper.paper_config("volatility_target_10")).replay_inputs(
        sub, strategy_id="sma_trend", strategy_version=version
    )
    last = run.portfolio.iloc[-1]
    for k in ("cash", "equity", "realized_pnl", "unrealized_pnl", "total_pnl", "cumulative_costs"):
        assert view[k] == last[k], k
    assert view["ledger_events"] == len(run.events)
    assert view["ledger_last_hash"] == run.events[-1]["hash"]
    assert view["exposure"] <= 1 and view["cash"] >= 0


def _page_script() -> None:
    import streamlit as st

    from app.dashboard.ui import pages
    from app.dashboard.ui.context import build_context

    dict((f.__name__, f) for _, f in pages.PAGES)[st.session_state["page"]](build_context())


def test_every_page_renders(data) -> None:  # type: ignore[no-untyped-def]
    from streamlit.testing.v1 import AppTest

    from app.dashboard.ui.pages import PAGES

    assert len(PAGES) == 14
    for title, f in PAGES:
        at = AppTest.from_function(_page_script, default_timeout=900)
        at.session_state["page"] = f.__name__
        at.run()
        assert not at.exception, (title, [e.value for e in at.exception])
        assert at.title and at.title[0].value, title
        text = " ".join(str(m.value) for m in at.markdown) + " ".join(
            str(i.value) for i in (*at.info, *at.warning, *at.error, *at.caption)
        )
        secret = Settings().postgres_password.get_secret_value()  # type: ignore[call-arg]
        assert secret not in text and "postgres_password" not in text.lower(), title
        rendered = " ".join(str(d.value) for d in at.dataframe) if at.dataframe else ""
        assert secret not in rendered, title
