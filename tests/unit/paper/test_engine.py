"""Paper-trading replay: Phase 8/11 equivalence, risk integration, accounting identities,
auditability, temporal integrity (future mutation, ordering, pending), determinism."""

import dataclasses
from datetime import date
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.backtest.config import BacktestConfig
from app.backtest.engine import Backtester, session_times
from app.data.calendar import TradingCalendar
from app.indicators.engine import IndicatorEngine
from app.paper.config import PaperConfig
from app.paper.engine import PaperRun, PaperTradingEngine, data_fingerprint
from app.paper.models import FILLED, PENDING, REJECTED
from app.risk.config import SCENARIOS
from app.risk.engine import RiskOverlay
from app.risk.manager import RiskManager
from tests.unit.risk.test_overlay import flat_indicators, make_bars
from tests.unit.strategies.helpers import session_walk, small_engine

CAL = TradingCalendar("XNYS")
OHLCV = ["open", "high", "low", "close", "volume"]


@pytest.fixture(scope="module")
def walk():  # type: ignore[no-untyped-def]
    bars = session_walk(300, seed=21)
    strat = small_engine().run(bars)
    ind = IndicatorEngine().analyze(bars[OHLCV]).values
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    states = {
        str(sid): pd.Series(g["state"].to_numpy(), index=pd.DatetimeIndex(g["bar_ts"]))
        for sid, g in strat.signals.groupby("strategy_id", sort=False)
    }
    return bars, strat, ind, times, states


def replay(bars, states, ind, times, config=None, sid="x"):  # type: ignore[no-untyped-def]
    fp = data_fingerprint(bars)
    return PaperTradingEngine(config).replay(
        bars, states, ind, times, strategy_id=sid, data_fp=fp
    )


def all_runs(walk, config=None) -> dict[str, PaperRun]:  # type: ignore[no-untyped-def]
    bars, _, ind, times, states = walk
    return {sid: replay(bars, st, ind, times, config, sid) for sid, st in states.items()}


# ── equivalence ──


def test_control_reproduces_phase8_exactly(walk) -> None:  # type: ignore[no-untyped-def]
    bars, strat, *_ = walk
    p8 = Backtester(BacktestConfig()).run(bars, strat)
    for sid, r in all_runs(walk).items():
        ref = p8[sid]
        for col in ("equity", "gross_equity", "position", "cash", "shares", "costs"):
            np.testing.assert_array_equal(r.equity[col].to_numpy(), ref.equity[col].to_numpy())
        f = r.fills
        assert len(f) == len(ref.fills) == ref.metrics["orders"]
        np.testing.assert_array_equal(f["executed_at"].to_numpy(), ref.fills["ts"].to_numpy())
        for a, b in (("price", "price"), ("notional", "notional"), ("total_cost", "total_cost")):
            np.testing.assert_array_equal(f[a].to_numpy(), ref.fills[b].to_numpy())
        assert r.metrics == ref.metrics


@pytest.mark.parametrize("risk", SCENARIOS, ids=lambda r: r.name)
def test_every_scenario_reproduces_phase11(walk, risk) -> None:  # type: ignore[no-untyped-def]
    bars, _, ind, times, states = walk
    for sid, st in states.items():
        p11 = RiskOverlay(risk, BacktestConfig()).run(bars, st, ind, times, strategy_id=sid)
        r = replay(bars, st, ind, times, PaperConfig(risk=risk), sid)
        np.testing.assert_array_equal(r.equity["equity"].to_numpy(), p11.equity["equity"].to_numpy())
        np.testing.assert_array_equal(
            r.equity["gross_equity"].to_numpy(), p11.equity["gross_equity"].to_numpy()
        )
        np.testing.assert_array_equal(
            r.decisions["risk_approved_target"].to_numpy(),
            p11.decisions["approved_exposure"].to_numpy(),
        )
        assert len(r.fills) == len(p11.fills)


# ── risk integration ──


def test_risk_manager_decides_every_bar_and_broker_gets_only_approved(walk) -> None:  # type: ignore[no-untyped-def]
    bars, _, ind, times, states = walk
    st = states["sma_trend"]
    with patch.object(RiskManager, "decide", autospec=True, side_effect=RiskManager.decide) as m:
        PaperTradingEngine(PaperConfig(risk=SCENARIOS[1]))._replay(
            bars, st, ind, times, "sma_trend", "1", "SPY", ""
        )
    assert m.call_count == len(bars)  # net replay only: one risk decision per bar

    r = replay(bars, st, ind, times, PaperConfig(risk=SCENARIOS[1]), "sma_trend")
    d = r.decisions
    assert d["strategy_state"].tolist() == st.tolist()  # strategy state unchanged
    long = d["strategy_state"] == "LONG"
    assert (d.loc[long, "strategy_requested_target"] == 1.0).all()
    assert (d.loc[long, "risk_approved_target"] == 0.5).all()
    assert d.loc[long, "intervention"].all() and (d.loc[long, "intervention_reason"] != "").all()
    rec = r.reconciliation.merge(d, on="decision_id", suffixes=("", "_d"))
    np.testing.assert_array_equal(rec["risk_approved_target"], rec["risk_approved_target_d"])
    filled = r.reconciliation[r.reconciliation["outcome"] == "filled"]
    assert filled["explanation"].str.contains("differs from strategy request").any()


def test_signals_are_never_modified(walk) -> None:  # type: ignore[no-untyped-def]
    _, strat, *_ = walk
    before = strat.signals.copy(deep=True)
    all_runs(walk, PaperConfig(risk=SCENARIOS[3]))
    pd.testing.assert_frame_equal(strat.signals, before)


# ── accounting ──


@pytest.mark.parametrize("risk", [SCENARIOS[0], SCENARIOS[1], SCENARIOS[4]], ids=lambda r: r.name)
def test_accounting_identities(walk, risk) -> None:  # type: ignore[no-untyped-def]
    initial = BacktestConfig().initial_capital
    for r in all_runs(walk, PaperConfig(risk=risk)).values():
        p = r.portfolio
        np.testing.assert_allclose(p["equity"], p["cash"] + p["position_market_value"], rtol=1e-12)
        np.testing.assert_allclose(
            p["equity"],
            initial + p["realized_pnl"] + p["unrealized_pnl"] - p["cumulative_costs"],
            rtol=1e-9,
        )
        assert (p["cash"] >= -1e-6).all() and (p["position_quantity"] >= 0).all()
        assert (p["gross_exposure"] <= 1 + 1e-9).all()
        assert p["cumulative_costs"].iloc[-1] == pytest.approx(r.fills["total_cost"].sum())
        assert r.equity["costs"].sum() == pytest.approx(r.fills["total_cost"].sum())
        np.testing.assert_allclose(
            r.fills[["commission", "spread_cost", "slippage_cost"]].sum(axis=1), r.fills["total_cost"]
        )


# ── auditability ──


def test_ids_unique_linked_and_fingerprinted(walk) -> None:  # type: ignore[no-untyped-def]
    for r in all_runs(walk, PaperConfig(risk=SCENARIOS[2])).values():
        d, o, f = r.decisions, r.orders, r.fills
        for frame, col in ((d, "decision_id"), (o, "order_id"), (f, "fill_id")):
            assert frame[col].is_unique
        assert set(o["decision_id"]) <= set(d["decision_id"])
        assert set(f["order_id"]) <= set(o["order_id"])
        assert set(f["order_id"]) == set(o.loc[o["status"] == FILLED, "order_id"])
        assert (d["config_fingerprint"] == r.config_fingerprint).all()
        assert (d["data_fingerprint"] == r.data_fingerprint).all() and len(r.data_fingerprint) == 16
        assert set(r.reconciliation["decision_id"]) <= set(d["decision_id"])
        joined = f.merge(o, on="order_id").merge(d, on="decision_id")
        assert (joined["executed_at"] == joined["scheduled_execution_at_x"]).all()
        assert (joined["executed_at"] > joined["observed_at"]).all()  # never before the decision


# ── temporal integrity ──


def test_final_bar_decision_is_pending() -> None:
    bars = make_bars([(100, 101, 99, 100)] * 6)
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    st = pd.Series(["FLAT"] * 5 + ["LONG"], index=bars.index)
    r = PaperTradingEngine().replay(bars, st, flat_indicators(bars), times, strategy_id="x")
    assert r.pending_order is not None and r.pending_order["status"] == PENDING
    assert r.pending_order["decision_id"] == r.decisions["decision_id"].iloc[-1]
    assert len(r.fills) == 0 and (r.portfolio["position_quantity"] == 0).all()


@pytest.mark.parametrize("what", ["price", "volume", "state", "indicator"])
def test_future_mutation_does_not_change_the_past(walk, what) -> None:  # type: ignore[no-untyped-def]
    bars, _, ind, times, states = walk
    cut = 200
    st, b2, i2 = states["rsi_momentum"].copy(), bars.copy(), ind.copy()
    if what == "price":
        b2.iloc[cut + 1 :, :4] *= 1.7
    elif what == "volume":
        b2.iloc[cut + 1 :, 4] = 1.0
    elif what == "state":
        st.iloc[cut + 1 :] = "FLAT"
    else:
        i2.iloc[cut + 1 :] = i2.iloc[cut + 1 :] * 3
    base = replay(bars, states["rsi_momentum"], ind, times, PaperConfig(risk=SCENARIOS[4]))
    mut = replay(b2, st, i2, times, PaperConfig(risk=SCENARIOS[4]))
    ts = bars.index[cut]
    cols = [c for c in base.decisions.columns if c != "data_fingerprint"]
    pd.testing.assert_frame_equal(
        base.decisions.loc[base.decisions["bar_ts"] <= ts, cols],
        mut.decisions.loc[mut.decisions["bar_ts"] <= ts, cols],
    )
    pd.testing.assert_frame_equal(
        base.portfolio[base.portfolio["ts"] <= ts], mut.portfolio[mut.portfolio["ts"] <= ts]
    )
    pd.testing.assert_frame_equal(
        base.fills[base.fills["executed_at"] <= ts], mut.fills[mut.fills["executed_at"] <= ts]
    )


def test_shuffled_bars_rejected(walk) -> None:  # type: ignore[no-untyped-def]
    bars, _, ind, times, states = walk
    shuffled = bars.sample(frac=1.0, random_state=3)
    with pytest.raises(ValueError, match="increasing time order"):
        replay(shuffled, states["sma_trend"], ind, times)


def test_missing_session_rejects_order_and_replay_continues() -> None:
    full = make_bars([(100, 101, 99, 100)] * 8, start=date(2024, 7, 8))
    bars = full.drop(full.index[2])  # the session the LONG decision is scheduled for is absent
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    assert times["effective_at"].iloc[1] == full.index[2]  # calendar, not "next row"
    st = pd.Series(["FLAT"] + ["LONG"] * 6, index=bars.index)
    r = PaperTradingEngine().replay(bars, st, flat_indicators(bars), times, strategy_id="x")
    rej = r.orders[r.orders["status"] == REJECTED]
    assert rej["rejection_reason"].tolist() == ["scheduled_session_missing_in_data"]
    assert r.portfolio["position_quantity"].iloc[2] == 0  # position unchanged by the rejection
    assert r.fills["executed_at"].iloc[0] == bars.index[3]  # next decision filled normally
    assert "rejected:" in r.reconciliation["outcome"].iloc[1]


def test_same_bar_execution_is_rejected() -> None:
    bars = make_bars([(100, 101, 99, 100)] * 5)
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    bad = times.copy()
    bad["observed_at"] = bad["effective_at"]  # control: decision claims to be observed at fill time
    st = pd.Series(["LONG"] * 5, index=bars.index)
    r = PaperTradingEngine().replay(bars, st, flat_indicators(bars), bad, strategy_id="x")
    assert (r.orders["status"] != FILLED).all() and len(r.fills) == 0
    assert set(r.orders.loc[r.orders["status"] == REJECTED, "rejection_reason"]) == {
        "execution_not_after_decision"
    }


# ── determinism ──


def test_replay_is_deterministic(walk) -> None:  # type: ignore[no-untyped-def]
    a = all_runs(walk, PaperConfig(risk=SCENARIOS[5]))
    b = all_runs(walk, PaperConfig(risk=SCENARIOS[5]))
    for sid in a:
        assert a[sid].run_id == b[sid].run_id
        for name in ("decisions", "orders", "fills", "portfolio", "reconciliation"):
            pd.testing.assert_frame_equal(getattr(a[sid], name), getattr(b[sid], name))


def test_run_id_depends_on_config_and_data(walk) -> None:  # type: ignore[no-untyped-def]
    bars, _, ind, times, states = walk
    st = states["buy_and_hold"]
    base = replay(bars, st, ind, times).run_id
    assert replay(bars, st, ind, times, PaperConfig(risk=SCENARIOS[1])).run_id != base
    other = bars.copy()
    other.iloc[0, 3] += 0.01
    assert replay(other, st, ind, times).run_id != base
    assert dataclasses.replace(PaperConfig(), cash_tolerance=1e-5).fingerprint() != (
        PaperConfig().fingerprint()
    )
