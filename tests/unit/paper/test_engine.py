"""Paper-trading replay: Phase 8/11 equivalence, risk integration (no bypass), accounting
identities, auditability, point-in-time IDs, temporal integrity (prefix, future mutation,
ordering, pending), determinism."""

import dataclasses
import math
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
from app.risk.manager import RiskDecision, RiskManager
from tests.unit.risk.test_overlay import flat_indicators, make_bars
from tests.unit.strategies.helpers import session_walk, small_engine

CAL = TradingCalendar("XNYS")
OHLCV = ["open", "high", "low", "close", "volume"]
RECORDS = ("decisions", "orders", "fills", "portfolio", "reconciliation", "trades")


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
    return PaperTradingEngine(config).replay(bars, states, ind, times, strategy_id=sid)


def all_runs(walk, config=None) -> dict[str, PaperRun]:  # type: ignore[no-untyped-def]
    bars, _, ind, times, states = walk
    return {sid: replay(bars, st, ind, times, config, sid) for sid, st in states.items()}


# ── equivalence ──

# The paper broker never books negative cash: Phase 8/11 entries can leave cash at ~-1.5e-11
# (exposure 1 + 2e-16); the paper broker shaves a few ulps off such buys (app.paper.broker).
# Paper and Phase 8/11 are therefore equal up to this residue, asserted at 1e-6 currency on a
# 100,000 account; everything discrete (dates, prices, counts, states) is asserted exactly.
USD = {"rtol": 0.0, "atol": 1e-6}
INTEGRAL = {"sessions", "completed_trades", "orders", "max_drawdown_duration_sessions"}


def test_control_reproduces_phase8(walk) -> None:  # type: ignore[no-untyped-def]
    bars, strat, *_ = walk
    p8 = Backtester(BacktestConfig()).run(bars, strat)
    for sid, r in all_runs(walk).items():
        ref = p8[sid]
        for col in ("equity", "gross_equity", "cash", "costs"):
            np.testing.assert_allclose(r.equity[col], ref.equity[col], **USD, err_msg=col)
        np.testing.assert_allclose(r.equity["shares"], ref.equity["shares"], rtol=1e-15, atol=0)
        np.testing.assert_allclose(r.equity["position"], ref.equity["position"], atol=1e-12)
        assert (r.equity["cash"] >= 0).all() and (r.equity["position"] <= 1).all()
        f = r.fills
        assert len(f) == len(ref.fills) == ref.metrics["orders"]
        np.testing.assert_array_equal(f["executed_at"].to_numpy(), ref.fills["ts"].to_numpy())
        np.testing.assert_array_equal(f["price"].to_numpy(), ref.fills["price"].to_numpy())
        for col in ("notional", "total_cost"):
            np.testing.assert_allclose(f[col], ref.fills[col], **USD)

        def col_of(df: pd.DataFrame, c: str) -> np.ndarray:
            return df[c].to_numpy(dtype=float)  # empty trade tables have object dtype

        for col in ("net_pnl", "gross_pnl"):
            np.testing.assert_allclose(col_of(r.trades, col), col_of(ref.trades, col), **USD)
        np.testing.assert_allclose(
            col_of(r.trades, "net_return"), col_of(ref.trades, "net_return"), atol=1e-14
        )
        np.testing.assert_array_equal(
            col_of(r.trades, "holding_sessions"), col_of(ref.trades, "holding_sessions")
        )
        assert r.metrics.keys() == ref.metrics.keys()
        for key, expected in ref.metrics.items():
            got = r.metrics[key]
            if isinstance(expected, float) and math.isnan(expected):
                assert isinstance(got, float) and math.isnan(got), (sid, key)  # stays undefined
            elif key in INTEGRAL:
                assert got == expected, (sid, key)
            else:
                assert got == pytest.approx(expected, rel=1e-9, abs=1e-6), (sid, key)


@pytest.mark.parametrize("risk", SCENARIOS, ids=lambda r: r.name)
def test_every_scenario_reproduces_phase11(walk, risk) -> None:  # type: ignore[no-untyped-def]
    bars, _, ind, times, states = walk
    for sid, st in states.items():
        p11 = RiskOverlay(risk, BacktestConfig()).run(bars, st, ind, times, strategy_id=sid)
        r = replay(bars, st, ind, times, PaperConfig(risk=risk), sid)
        for col in ("equity", "gross_equity", "cash", "costs"):
            np.testing.assert_allclose(r.equity[col], p11.equity[col], **USD, err_msg=col)
        np.testing.assert_allclose(r.equity["shares"], p11.equity["shares"], rtol=1e-15, atol=0)
        np.testing.assert_array_equal(
            r.decisions["risk_approved_target"].to_numpy(),
            p11.decisions["approved_exposure"].to_numpy(),
        )
        assert len(r.fills) == len(p11.fills)
        np.testing.assert_array_equal(r.fills["executed_at"], p11.fills["ts"].to_numpy())
        np.testing.assert_allclose(r.fills["notional"], p11.fills["notional"], **USD)


# ── risk integration: no bypass ──


def test_risk_manager_decides_every_bar_and_broker_gets_only_approved(walk) -> None:  # type: ignore[no-untyped-def]
    bars, _, ind, times, states = walk
    st = states["sma_trend"]
    with patch.object(RiskManager, "decide", autospec=True, side_effect=RiskManager.decide) as m:
        replay(bars, st, ind, times, PaperConfig(risk=SCENARIOS[1]), "sma_trend")
    assert m.call_count == len(bars)  # net run only: one risk decision per bar

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


def test_raw_strategy_signal_cannot_become_a_fill(walk) -> None:  # type: ignore[no-untyped-def]
    """The strategy is LONG on many bars, but the risk layer (patched to veto everything)
    approves 0: no fill may happen. Only RiskManager output reaches the broker."""
    bars, _, ind, times, states = walk
    st = states["buy_and_hold"]
    assert (st == "LONG").sum() > 100

    def veto(self, **kw):  # type: ignore[no-untyped-def]
        return RiskDecision(
            kw["requested"], 0.0, 0.0, True, ("veto",), "NORMAL", 0.0, 1.0, math.nan, False, False
        )

    with patch.object(RiskManager, "decide", veto):
        r = replay(bars, st, ind, times)
    assert len(r.fills) == 0 and len(r.orders) == 0
    assert (r.portfolio["position_quantity"] == 0).all()
    assert (r.decisions["risk_approved_target"] == 0).all()


def test_signals_are_never_modified(walk) -> None:  # type: ignore[no-untyped-def]
    _, strat, *_ = walk
    before = strat.signals.copy(deep=True)
    all_runs(walk, PaperConfig(risk=SCENARIOS[3]))
    pd.testing.assert_frame_equal(strat.signals, before)


# ── accounting ──


@pytest.mark.parametrize(
    "risk", [SCENARIOS[0], SCENARIOS[1], SCENARIOS[2], SCENARIOS[4]], ids=lambda r: r.name
)
def test_accounting_identities(walk, risk) -> None:  # type: ignore[no-untyped-def]
    initial = BacktestConfig().initial_capital
    for r in all_runs(walk, PaperConfig(risk=risk)).values():
        p = r.portfolio
        np.testing.assert_array_equal(
            p["equity"], p["cash"] + p["position_quantity"] * p["mark_price"]
        )
        np.testing.assert_array_equal(
            p["position_market_value"], p["position_quantity"] * p["mark_price"]
        )
        np.testing.assert_array_equal(p["total_pnl"], p["realized_pnl"] + p["unrealized_pnl"])
        np.testing.assert_allclose(p["total_pnl"], p["equity"] - initial, rtol=0, atol=1e-7)
        flat = p["position_quantity"] == 0
        assert (p.loc[flat, "unrealized_pnl"] == 0).all() and (p.loc[flat, "cost_basis"] == 0).all()
        assert (p["cash"] >= 0).all() and (p["position_quantity"] >= 0).all()
        assert (p["gross_exposure"] <= 1).all()
        assert p["cumulative_costs"].iloc[-1] == pytest.approx(r.fills["total_cost"].sum())
        assert r.equity["costs"].sum() == pytest.approx(r.fills["total_cost"].sum())
        np.testing.assert_allclose(
            r.fills[["commission", "spread_cost", "slippage_cost"]].sum(axis=1),
            r.fills["total_cost"],
        )
        assert r.fills["realized_pnl"].sum() == pytest.approx(p["realized_pnl"].iloc[-1])
        if p["position_quantity"].iloc[-1] == 0:  # flat: every round trip is realized
            assert r.trades["net_pnl"].sum() == pytest.approx(p["realized_pnl"].iloc[-1])


# ── auditability / identity ──


def test_ids_unique_linked_and_point_in_time(walk) -> None:  # type: ignore[no-untyped-def]
    for r in all_runs(walk, PaperConfig(risk=SCENARIOS[2])).values():
        d, o, f, p = r.decisions, r.orders, r.fills, r.portfolio
        for frame, col in ((d, "decision_id"), (o, "order_id"), (f, "fill_id"), (p, "snapshot_id")):
            assert frame[col].is_unique
            assert frame[col].str.startswith(r.account_id + "-").all()
        tags = d["bar_ts"].dt.tz_convert("America/New_York").dt.strftime("%Y%m%d")
        assert (d["decision_id"] == r.account_id + "-D" + tags).all()
        assert set(o["decision_id"]) <= set(d["decision_id"])
        assert set(f["order_id"]) <= set(o["order_id"])
        assert set(f["order_id"]) == set(o.loc[o["status"] == FILLED, "order_id"])
        assert (d["config_fingerprint"] == r.config_fingerprint).all()
        assert set(r.reconciliation["decision_id"]) <= set(d["decision_id"])
        joined = f.merge(o, on="order_id").merge(d, on="decision_id")
        assert (joined["executed_at"] == joined["scheduled_execution_at_x"]).all()
        assert (joined["executed_at"] > joined["observed_at"]).all()  # never before decision
        assert len(r.events) == len(set(e["event_id"] for e in r.events))


def test_account_id_depends_on_config_and_strategy_not_data(walk) -> None:  # type: ignore[no-untyped-def]
    bars, _, ind, times, states = walk
    st = states["buy_and_hold"]
    base = replay(bars, st, ind, times).account_id
    assert replay(bars, st, ind, times, PaperConfig(risk=SCENARIOS[1])).account_id != base
    assert replay(bars, st, ind, times, sid="y").account_id != base
    other = bars.copy()
    other.iloc[50:, :4] *= 1.3  # different data -> same account, same identities
    run_other = replay(other, st, ind, times)
    assert run_other.account_id == base
    assert run_other.data_fingerprint != data_fingerprint(bars)  # report metadata only
    assert dataclasses.replace(PaperConfig(), cash_tolerance=1e-5).fingerprint() != (
        PaperConfig().fingerprint()
    )


# ── temporal integrity ──


def test_final_bar_entry_decision_is_pending() -> None:
    bars = make_bars([(100, 101, 99, 100)] * 6)
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    st = pd.Series(["FLAT"] * 5 + ["LONG"], index=bars.index)
    r = PaperTradingEngine().replay(bars, st, flat_indicators(bars), times, strategy_id="x")
    assert r.pending_order is not None and r.pending_order["status"] == PENDING
    assert r.pending_order["kind"] == "entry"
    assert r.pending_order["decision_id"] == r.decisions["decision_id"].iloc[-1]
    assert len(r.fills) == 0 and (r.portfolio["position_quantity"] == 0).all()
    assert r.orders["status"].tolist() == [PENDING]


def _known_through(r: PaperRun, ts: pd.Timestamp) -> dict[str, pd.DataFrame]:
    cols = {
        "decisions": "bar_ts",
        "fills": "executed_at",
        "portfolio": "ts",
        "reconciliation": "ts",
        "trades": "exit_time",
    }
    out = {k: getattr(r, k)[getattr(r, k)[c] <= ts].reset_index(drop=True) for k, c in cols.items()}
    o = r.orders
    out["orders"] = o[(o["scheduled_execution_at"] <= ts) & (o["status"] != PENDING)].reset_index(
        drop=True
    )
    out["equity"] = r.equity.loc[:ts]
    out["events"] = pd.DataFrame([e for e in r.events if pd.Timestamp(e["session"]) <= ts])
    return out


def _assert_same_through(a: PaperRun, b: PaperRun, ts: pd.Timestamp) -> None:
    ka, kb = _known_through(a, ts), _known_through(b, ts)
    for name in ka:
        pd.testing.assert_frame_equal(ka[name], kb[name], obj=name)


def test_prefix_equals_full_history(walk) -> None:  # type: ignore[no-untyped-def]
    bars, _, ind, times, states = walk
    cfg = PaperConfig(risk=SCENARIOS[2])  # volatility target: rebalances
    for sid in ("rsi_momentum", "sma_trend"):
        full = replay(bars, states[sid], ind, times, cfg, sid)
        for cut in (60, 151, 230):
            part = replay(bars.iloc[: cut + 1], states[sid], ind, times, cfg, sid)
            _assert_same_through(part, full, bars.index[cut])


@pytest.mark.parametrize("what", ["price", "volume", "adj_close", "state", "indicator"])
def test_future_mutation_does_not_change_the_past(walk, what) -> None:  # type: ignore[no-untyped-def]
    bars, _, ind, times, states = walk
    cut = 200
    st, b2, i2 = states["rsi_momentum"].copy(), bars.copy(), ind.copy()
    if what == "price":
        b2.iloc[cut + 1 :, :4] *= 1.7
    elif what == "volume":
        b2.iloc[cut + 1 :, 4] = 1.0
    elif what == "adj_close":
        b2 = b2.assign(adj_close=b2["close"])
        b2.iloc[cut + 1 :, b2.columns.get_loc("adj_close")] *= 0.1
    elif what == "state":
        st.iloc[cut + 1 :] = "FLAT"
    else:
        i2.iloc[cut + 1 :] = i2.iloc[cut + 1 :] * 3
    cfg = PaperConfig(risk=SCENARIOS[4])
    base = replay(bars, states["rsi_momentum"], ind, times, cfg)
    mut = replay(b2, st, i2, times, cfg)
    _assert_same_through(base, mut, bars.index[cut])  # includes every ID and ledger event


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


def test_decision_not_before_its_execution_is_rejected() -> None:
    """Control: an observation claiming its decision is effective at (not after) its own close
    would let a decision execute on the bar that produced it; the trader refuses it."""
    bars = make_bars([(100, 101, 99, 100)] * 5)
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    st = pd.Series(["LONG"] * 5, index=bars.index)
    bad = times.assign(effective_at=times["observed_at"])
    with pytest.raises(ValueError, match="effective_at must be after"):
        PaperTradingEngine().replay(bars, st, flat_indicators(bars), bad, strategy_id="x")


# ── determinism ──


def test_replay_is_deterministic(walk) -> None:  # type: ignore[no-untyped-def]
    a = all_runs(walk, PaperConfig(risk=SCENARIOS[5]))
    b = all_runs(walk, PaperConfig(risk=SCENARIOS[5]))
    for sid in a:
        assert a[sid].account_id == b[sid].account_id
        assert a[sid].events == b[sid].events
        for name in RECORDS:
            pd.testing.assert_frame_equal(getattr(a[sid], name), getattr(b[sid], name))
