"""Risk overlay: Phase 8 equivalence, requested vs approved, sizing/limits in the simulation,
close-based stops and OHLC ambiguity, drawdown lock, point-in-time guarantees, determinism."""

import dataclasses
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.backtest.config import BacktestConfig
from app.backtest.engine import Backtester, session_times
from app.data.calendar import TradingCalendar
from app.indicators.engine import IndicatorEngine
from app.risk.config import SCENARIOS, DrawdownSpec, RiskConfig, SizingSpec, StopSpec
from app.risk.engine import RiskOverlay, RiskRun
from tests.unit.strategies.helpers import XNYS, session_walk, small_engine

CAL = TradingCalendar("XNYS")
FREE = BacktestConfig(
    initial_capital=10_000.0, commission_per_trade=0.0, spread_bps=0.0, slippage_bps=0.0
)
L, F, I = "LONG", "FLAT", "INSUFFICIENT_DATA"  # noqa: E741


def make_bars(
    rows: list[tuple[float, float, float, float]], start: date = date(2024, 7, 1)
) -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        XNYS.sessions(start, date(2030, 1, 1))["open_utc"][: len(rows)], name="ts"
    )
    o, h, lo, c = (list(x) for x in zip(*rows, strict=True))
    return pd.DataFrame(
        {"open": o, "high": h, "low": lo, "close": c, "volume": 1e6, "adj_close": c}, index=idx
    )


def flat_indicators(bars: pd.DataFrame, vol: float = 0.2, atr: float = 2.0) -> pd.DataFrame:
    return pd.DataFrame({"realized_vol_20": vol, "atr_14": atr}, index=bars.index)


def run(
    bars: pd.DataFrame,
    states: list[str],
    risk: RiskConfig,
    ind: pd.DataFrame | None = None,
    bt: BacktestConfig = FREE,
) -> RiskRun:
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    return RiskOverlay(risk, bt).run(
        bars,
        pd.Series(states, index=bars.index),
        ind if ind is not None else flat_indicators(bars),
        times,
        strategy_id="x",
    )


@pytest.fixture(scope="module")
def walk_inputs():  # type: ignore[no-untyped-def]
    bars = session_walk(300, seed=21)
    bars = bars.assign(adj_close=bars["close"])
    strat = small_engine().run(bars.drop(columns=["adj_close"]))
    ind = IndicatorEngine().analyze(bars[["open", "high", "low", "close", "volume"]]).values
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    return bars, strat, ind, times


def overlay_all(
    bars, strat, ind, times, risk: RiskConfig, bt: BacktestConfig | None = None
) -> dict[str, RiskRun]:  # type: ignore[no-untyped-def]
    out = {}
    for sid, g in strat.signals.groupby("strategy_id", sort=False):
        st = pd.Series(g["state"].to_numpy(), index=pd.DatetimeIndex(g["bar_ts"]))
        out[str(sid)] = RiskOverlay(risk, bt or BacktestConfig()).run(
            bars, st, ind, times, strategy_id=str(sid)
        )
    return out


# ── equivalence with Phase 8 ──


def test_control_reproduces_phase8_exactly(walk_inputs) -> None:  # type: ignore[no-untyped-def]
    bars, strat, ind, times = walk_inputs
    p8 = Backtester(BacktestConfig()).run(bars, strat)
    ctl = overlay_all(bars, strat, ind, times, SCENARIOS[0])
    for sid, r in ctl.items():
        np.testing.assert_array_equal(
            r.equity["equity"].to_numpy(), p8[sid].equity["equity"].to_numpy()
        )
        np.testing.assert_array_equal(
            r.equity["gross_equity"].to_numpy(), p8[sid].equity["gross_equity"].to_numpy()
        )
        assert r.metrics["orders"] == p8[sid].metrics["orders"]
        assert r.diagnostics["interventions"] == 0


def test_requested_is_the_unmodified_strategy_signal(walk_inputs) -> None:  # type: ignore[no-untyped-def]
    bars, strat, ind, times = walk_inputs
    before = strat.signals.copy(deep=True)
    runs = overlay_all(bars, strat, ind, times, SCENARIOS[1])
    pd.testing.assert_frame_equal(strat.signals, before)  # signals never modified
    for sid, r in runs.items():
        states = strat.signals[strat.signals["strategy_id"] == sid]["state"].tolist()
        assert r.decisions["strategy_state"].tolist() == states
        long = r.decisions["strategy_state"] == L
        assert (r.decisions.loc[long, "requested_exposure"] == 1.0).all()
        assert (r.decisions.loc[long, "approved_exposure"] == 0.5).all()
        assert (r.decisions.loc[long, "intervention"]).all()


# ── sizing and limits in the simulation ──


@pytest.mark.parametrize("risk", SCENARIOS, ids=lambda r: r.name)
def test_no_leverage_no_negative_cash(walk_inputs, risk: RiskConfig) -> None:  # type: ignore[no-untyped-def]
    bars, strat, ind, times = walk_inputs
    for r in overlay_all(bars, strat, ind, times, risk).values():
        assert (r.equity["position"] <= 1.0 + 1e-9).all()
        assert (r.equity["cash"] >= -1e-6).all()
        assert (r.equity["shares"] >= 0).all()
        f = r.fills
        if len(f):
            assert (f["ts"] > f["signal_observed_at"]).all()
            np.testing.assert_array_equal(
                f["price"].to_numpy(), bars.loc[f["ts"], "open"].to_numpy()
            )


def test_fixed_fraction_is_maintained_within_band() -> None:
    rows = [(100 * 1.01**i, 100 * 1.01**i + 1, 100 * 1.01**i - 1, 100 * 1.01**i) for i in range(80)]
    bars = make_bars(rows)
    r = run(bars, [L] * 80, RiskConfig("ff", sizing=SizingSpec("fixed_fraction", fraction=0.5)))
    # pre-trade exposure at each open (yesterday's shares at today's open price)
    eq_open = r.equity["cash"].shift(1) + r.equity["shares"].shift(1) * r.equity["open"]
    exposure_open = r.equity["shares"].shift(1) * r.equity["open"] / eq_open
    drifted = exposure_open.iloc[2:] >= 0.5 + 0.10
    assert drifted.any()  # the rising price pushes exposure above the band...
    assert r.equity["executed"].iloc[2:][drifted].all()  # ...and every such open rebalances
    post = (
        r.equity["shares"]
        * r.equity["open"]
        / (r.equity["cash"] + r.equity["shares"] * r.equity["open"])
    )
    assert (post.iloc[1:] < 0.5 + 0.10).all()  # after the open, exposure is back within the band


# ── stops and daily OHLC ambiguity ──

ENTRY = [(100, 101, 99, 100), (100, 101, 99, 100)]  # LONG decided bar 0 -> fill at open of bar 1


def test_stop_not_touched() -> None:
    bars = make_bars([*ENTRY, (100, 101, 97, 98), (98, 99, 96, 97)])
    r = run(
        bars, [L] * 4, RiskConfig("s", stop=StopSpec("atr", atr_multiple=3.0))
    )  # level 100-6 = 94
    assert r.decisions["stop_triggered"].sum() == 0 and len(r.fills) == 1


def test_stop_triggered_on_close_exits_next_open() -> None:
    bars = make_bars([*ENTRY, (99, 99, 93, 93.5), (92, 93, 90, 91), (91, 92, 90, 91)])
    r = run(bars, [L] * 5, RiskConfig("s", stop=StopSpec("atr", atr_multiple=3.0)))
    d = r.decisions.set_index("bar_ts")
    assert d.iloc[2]["stop_level"] == 94 and d.iloc[2]["stop_triggered"]
    assert d.iloc[2]["approved_exposure"] == 0 and "stop_triggered" in d.iloc[2]["reasons"]
    exit_fill = r.fills.iloc[-1]
    assert (
        exit_fill["side"] == "sell"
        and exit_fill["ts"] == bars.index[3]
        and exit_fill["price"] == 92
    )
    assert r.trades.iloc[0]["exit_reason"] == "stop_triggered"


def test_intraday_touch_with_close_above_is_not_an_exit() -> None:
    # low 93 < 94 but close 96 > 94: ordering unknown -> touch flagged, no exit
    bars = make_bars([*ENTRY, (99, 102, 93, 96), (96, 97, 95, 96)])
    r = run(bars, [L] * 4, RiskConfig("s", stop=StopSpec("atr", atr_multiple=3.0)))
    assert (
        r.decisions["stop_intraday_touch"].sum() == 1 and r.decisions["stop_triggered"].sum() == 0
    )
    assert len(r.fills) == 1


def test_ambiguous_bar_high_and_low_both_extreme() -> None:
    # huge range: high far above, low far below the stop, close above it -> still no exit
    bars = make_bars([*ENTRY, (100, 130, 80, 95), (95, 96, 94.5, 95)])
    r = run(bars, [L] * 4, RiskConfig("s", stop=StopSpec("atr", atr_multiple=3.0)))
    assert len(r.fills) == 1 and r.decisions.iloc[2]["stop_intraday_touch"]


def test_stop_and_strategy_exit_on_same_close() -> None:
    bars = make_bars([*ENTRY, (99, 99, 92, 93), (92, 93, 90, 91)])
    r = run(bars, [L, L, F, F], RiskConfig("s", stop=StopSpec("atr", atr_multiple=3.0)))
    assert r.decisions.iloc[2]["stop_triggered"]
    assert len(r.fills) == 2  # one exit only, at the next open
    assert r.fills.iloc[1]["ts"] == bars.index[3]


def test_reentry_blocked_until_signal_reset() -> None:
    rows = [
        *ENTRY,
        (99, 99, 92, 93),
        (93, 95, 92, 94),
        (94, 96, 93, 95),
        (95, 96, 94, 95),
        (95, 97, 94, 96),
        (96, 97, 95, 96),
    ]
    states = [L, L, L, L, F, L, L, L]
    r = run(
        bars := make_bars(rows), states, RiskConfig("s", stop=StopSpec("atr", atr_multiple=3.0))
    )
    reasons = r.decisions["reasons"].tolist()
    assert "stop_lockout_until_signal_reset" in reasons[3]
    sides = r.fills["side"].tolist()
    assert sides == ["buy", "sell", "buy"]  # re-entry only after the FLAT at bar 4
    assert r.fills.iloc[2]["ts"] == bars.index[6]


def test_fixed_pct_stop() -> None:
    bars = make_bars([*ENTRY, (99, 99, 89, 89.5), (89, 90, 88, 89)])
    r = run(bars, [L] * 4, RiskConfig("s", stop=StopSpec("fixed_pct", pct=0.10)))
    assert (
        r.decisions.iloc[2]["stop_level"] == pytest.approx(90)
        and r.decisions.iloc[2]["stop_triggered"]
    )


# ── drawdown lock ──


def _decline_then_recover() -> pd.DataFrame:
    path = [100, 100, 95, 88, 78, 75, 74, 76, 78, 80, 82, 84, 86]
    return make_bars([(p, p + 0.5, p - 0.5, p) for p in path])


def test_drawdown_force_flat_and_cooldown() -> None:
    bars = _decline_then_recover()
    risk = RiskConfig(
        "dd", drawdown=DrawdownSpec(enabled=True, warning=0.10, lock=0.20, cooldown_sessions=3)
    )
    r = run(bars, [L] * len(bars), risk)
    states = r.equity["risk_state"].tolist()
    assert "WARNING" in states and "LOCKED" in states
    lock_at = states.index("LOCKED")
    assert "drawdown_lock_flat" in r.decisions.iloc[lock_at]["reasons"]
    assert r.equity["position"].iloc[lock_at + 1] == 0  # flat from the next open
    assert r.fills["side"].tolist()[:2] == ["buy", "sell"]
    assert r.fills["side"].tolist()[-1] == "buy"  # re-entry after the cooldown


def test_drawdown_block_entries_keeps_position() -> None:
    bars = _decline_then_recover()
    risk = RiskConfig(
        "dd", drawdown=DrawdownSpec(enabled=True, action="block_entries", cooldown_sessions=3)
    )
    r = run(bars, [L] * len(bars), risk)
    assert len(r.fills) == 1 and (r.equity["position"].iloc[1:] > 0).all()


# ── point-in-time ──


def test_prefix_equals_full_for_every_scenario(walk_inputs) -> None:  # type: ignore[no-untyped-def]
    bars, *_ = walk_inputs
    for risk in SCENARIOS:
        full = _overlay_from_bars(bars, risk)
        for cut in (120, 200, 260):
            part = _overlay_from_bars(bars.iloc[: cut + 1], risk)
            for sid in full:
                pd.testing.assert_frame_equal(
                    part[sid].equity, full[sid].equity.iloc[: cut + 1], obj=f"{risk.name}:{sid}"
                )
                pd.testing.assert_frame_equal(
                    part[sid].decisions, full[sid].decisions.iloc[: cut + 1]
                )


def _overlay_from_bars(bars: pd.DataFrame, risk: RiskConfig) -> dict[str, RiskRun]:
    ohlcv = bars[["open", "high", "low", "close", "volume"]]
    strat = small_engine().run(ohlcv)
    ind = IndicatorEngine().analyze(ohlcv).values
    return overlay_all(bars, strat, ind, session_times(pd.DatetimeIndex(bars.index), CAL), risk)


@pytest.mark.parametrize("what", ["prices", "volume"])
def test_future_bar_mutation(walk_inputs, what: str) -> None:  # type: ignore[no-untyped-def]
    bars, *_ = walk_inputs
    cut = 200
    altered = bars.copy()
    rng = np.random.default_rng(3)
    cols = ["open", "high", "low", "close"] if what == "prices" else ["volume"]
    for col in cols:
        altered.iloc[cut + 1 :, altered.columns.get_loc(col)] *= rng.uniform(
            0.5, 1.5, len(bars) - cut - 1
        )
    altered["high"] = altered[["open", "high", "low", "close"]].max(axis=1)
    altered["low"] = altered[["open", "high", "low", "close"]].min(axis=1)
    for risk in (SCENARIOS[2], SCENARIOS[4], SCENARIOS[5]):
        a, b = _overlay_from_bars(bars, risk), _overlay_from_bars(altered, risk)
        for sid in a:
            pd.testing.assert_frame_equal(
                a[sid].equity.iloc[: cut + 1], b[sid].equity.iloc[: cut + 1]
            )
            pd.testing.assert_frame_equal(
                a[sid].decisions.iloc[: cut + 1], b[sid].decisions.iloc[: cut + 1]
            )


@pytest.mark.parametrize("what", ["indicators", "states"])
def test_future_input_mutation(walk_inputs, what: str) -> None:  # type: ignore[no-untyped-def]
    bars, strat, ind, times = walk_inputs
    cut = bars.index[200]
    states = pd.Series(
        strat.signals[strat.signals["strategy_id"] == "sma_trend"]["state"].to_numpy(),
        index=bars.index,
    )
    ind2, states2 = ind.copy(), states.copy()
    if what == "indicators":  # future volatility / ATR (and therefore any regime-like input)
        ind2.loc[ind2.index > cut, ["realized_vol_20", "atr_14"]] *= 5.0
    else:
        states2[states2.index > cut] = np.where(states2[states2.index > cut] == L, F, L)
    for risk in SCENARIOS:
        a = RiskOverlay(risk).run(bars, states, ind, times, strategy_id="x")
        b = RiskOverlay(risk).run(bars, states2, ind2, times, strategy_id="x")
        pd.testing.assert_frame_equal(a.equity.loc[:cut], b.equity.loc[:cut])
        pd.testing.assert_frame_equal(
            a.decisions[a.decisions["bar_ts"] <= cut], b.decisions[b.decisions["bar_ts"] <= cut]
        )


def test_leaky_volatility_input_is_detected(walk_inputs) -> None:  # type: ignore[no-untyped-def]
    """Control: sizing on TOMORROW's volatility breaks prefix equality."""
    bars, strat, ind, times = walk_inputs
    states = pd.Series(
        strat.signals[strat.signals["strategy_id"] == "buy_and_hold"]["state"].to_numpy(),
        index=bars.index,
    )
    leaky = ind.assign(realized_vol_20=ind["realized_vol_20"].shift(-1))
    risk = SCENARIOS[2]
    full = RiskOverlay(risk).run(bars, states, leaky, times, strategy_id="x")
    part_ind = (
        IndicatorEngine()
        .analyze(bars.iloc[:201][["open", "high", "low", "close", "volume"]])
        .values
    )
    part_leaky = part_ind.assign(realized_vol_20=part_ind["realized_vol_20"].shift(-1))
    part = RiskOverlay(risk).run(
        bars.iloc[:201], states.iloc[:201], part_leaky, times.iloc[:201], strategy_id="x"
    )
    assert not part.decisions.equals(full.decisions.iloc[:201])


# ── determinism / errors ──


def test_deterministic(walk_inputs) -> None:  # type: ignore[no-untyped-def]
    bars, strat, ind, times = walk_inputs
    for risk in SCENARIOS:
        a = overlay_all(bars, strat, ind, times, risk)
        b = overlay_all(bars, strat, ind, times, risk)
        for sid in a:
            pd.testing.assert_frame_equal(a[sid].equity, b[sid].equity)
            pd.testing.assert_frame_equal(a[sid].decisions, b[sid].decisions)
            assert a[sid].risk_fingerprint == b[sid].risk_fingerprint


def test_missing_session_rejected() -> None:
    bars = make_bars([(100, 101, 99, 100)] * 5)
    bars = bars.drop(bars.index[2])
    with pytest.raises(ValueError, match="next exchange session"):
        run(bars, [F, L, L, L], RiskConfig("c"))


def test_gross_track_uses_same_decision_times() -> None:
    bars = make_bars([(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)])
    states = ([L] * 5 + [F] * 5) * 3
    costly = dataclasses.replace(FREE, commission_per_trade=1.0, slippage_bps=10.0)
    r = run(
        bars, states, RiskConfig("ff", sizing=SizingSpec("fixed_fraction", fraction=0.5)), bt=costly
    )
    assert r.metrics["gross_cumulative_return"] > r.metrics["cumulative_return"]
    assert r.metrics["total_costs"] == pytest.approx(r.fills["total_cost"].sum())
