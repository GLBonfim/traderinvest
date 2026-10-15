"""Convention A for the start of a paper window (approved after the Step 1 audit of ADR-0034).

`start` is the first result session. When a session exists before it, the decision at its
close (`PaperTrader.prime`: request rebuilt causally from the history, INSUFFICIENT_DATA
carried, fresh capital, no position or stop, monitors untouched) is executed at the first
open of the window, exactly as a research window (`RiskOverlay.run(start_index=k)`). It is
recorded as a decision plus a reference snapshot (ledger schema unchanged) and never counted
as a result session. Synthetic data, in-memory engines and `tmp_path` only.
"""

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.backtest.config import BacktestConfig
from app.backtest.engine import session_times
from app.backtest.metrics import returns_from_equity
from app.dashboard.services import paper as paper_svc
from app.paper.config import PaperConfig
from app.paper.engine import PaperRun, PaperTradingEngine, build_inputs
from app.paper.ledger import reconstruct_snapshots
from app.paper.store import PaperStore
from app.paper.trader import BarInput, PaperTrader, split_at_start
from app.risk.config import RiskConfig
from app.risk.engine import RiskOverlay, RiskRun
from app.strategies.base import FLAT, INSUFFICIENT_DATA, LONG
from tests.unit.risk.test_adr0034_baseline import (
    ATR3,
    CONTROL,
    DD20,
    drawdown_path_bars,
    stop_path_bars,
)
from tests.unit.risk.test_overlay import CAL, FREE, flat_indicators, make_bars

L, F, I = LONG, FLAT, INSUFFICIENT_DATA  # noqa: E741
COSTS = PaperConfig(risk=CONTROL).backtest  # the default Phase 8 costs
USD_ATOL = 1e-6
K = 4


def ramp(n: int = 12) -> pd.DataFrame:
    return make_bars([(100 + i, 101 + i, 99 + i, 100 + i) for i in range(n)])


def day(bars: pd.DataFrame, i: int) -> date:
    return pd.Timestamp(bars.index[i]).tz_convert("America/New_York").date()


def research(
    bars: pd.DataFrame, states: list[str], risk: RiskConfig, k: int, bt: BacktestConfig = FREE
) -> RiskRun:
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    return RiskOverlay(risk, bt).run(
        bars,
        pd.Series(states, index=bars.index),
        flat_indicators(bars),
        times,
        strategy_id="x",
        start_index=k,
    )


def paper(
    bars: pd.DataFrame,
    states: list[str],
    risk: RiskConfig,
    start: date | None,
    bt: BacktestConfig = FREE,
) -> PaperRun:
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    engine = PaperTradingEngine(PaperConfig(risk=risk, backtest=bt))
    series = pd.Series(states, index=bars.index)
    return engine.replay(bars, series, flat_indicators(bars), times, strategy_id="x", start=start)


def inputs(bars: pd.DataFrame, states: list[str]) -> list[BarInput]:
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    return build_inputs(bars, pd.Series(states, index=bars.index), flat_indicators(bars), times)


def assert_same_as_research(p: PaperRun, r: RiskRun) -> None:
    """Same criterion as test_every_scenario_reproduces_phase11."""
    assert list(p.equity.index) == list(r.equity.index)
    for col in ("equity", "cash", "costs"):
        np.testing.assert_allclose(
            p.equity[col], r.equity[col], rtol=0.0, atol=USD_ATOL, err_msg=col
        )
    np.testing.assert_allclose(p.equity["shares"], r.equity["shares"], rtol=1e-15, atol=0)
    assert list(p.decisions["bar_ts"]) == list(r.decisions["bar_ts"])
    np.testing.assert_array_equal(
        p.decisions["strategy_requested_target"].to_numpy(),
        r.decisions["requested_exposure"].to_numpy(),
    )
    np.testing.assert_array_equal(
        p.decisions["risk_approved_target"].to_numpy(), r.decisions["approved_exposure"].to_numpy()
    )
    assert len(p.fills) == len(r.fills)
    np.testing.assert_array_equal(p.fills["executed_at"], r.fills["ts"].to_numpy())
    np.testing.assert_array_equal(p.fills["price"].to_numpy(), r.fills["price"].to_numpy())
    assert p.portfolio["risk_state"].tolist() == r.equity["risk_state"].tolist()


# The probe cases of the start-semantics audit (k = 4); research entry -> paper entry.
CASES = {
    "long_before_start": ([L] * 12, 4),
    "transition_at_start_minus_1": ([F, F, F, L] + [L] * 8, 4),
    "new_signal_at_start": ([F] * 4 + [L] * 8, 5),
    "insufficient_carries_long": ([F, L, L, L, I, I] + [L] * 6, 4),
    "insufficient_at_start_minus_1": ([F, F, L, I, I] + [L] * 7, 4),
}


# ── 1. temporal parity with the research engine ──


@pytest.mark.parametrize("risk", [CONTROL, ATR3, DD20], ids=lambda r: r.name)
@pytest.mark.parametrize("case", list(CASES))
def test_window_start_reproduces_the_research_window(case: str, risk: RiskConfig) -> None:
    states, entry = CASES[case]
    bars = ramp()
    p = paper(bars, states, risk, day(bars, K))
    assert_same_as_research(p, research(bars, states, risk, K))
    assert p.fills.iloc[0]["executed_at"] == bars.index[entry]


@pytest.mark.parametrize("risk", [CONTROL, ATR3, DD20], ids=lambda r: r.name)
def test_window_start_with_costs_reproduces_the_research_window(risk: RiskConfig) -> None:
    bars = ramp()
    p = paper(bars, [L] * 12, risk, day(bars, K), bt=COSTS)
    assert_same_as_research(p, research(bars, [L] * 12, risk, K, bt=COSTS))
    assert p.fills.iloc[0]["total_cost"] > 0


# ── 2. LONG before the window: a new entry, nothing inherited ──


def test_long_before_the_window_is_a_new_entry_at_the_first_open() -> None:
    bars = ramp()
    p = paper(bars, [L] * 12, ATR3, day(bars, K), bt=COSTS)
    ref = p.decisions.iloc[0]
    assert ref["bar_ts"] == bars.index[K - 1]
    assert ref["pending_kind"] == "entry" and ref["risk_approved_target"] == 1.0
    fill = p.fills.iloc[0]
    assert fill["executed_at"] == bars.index[K] and fill["price"] == 100 + K
    assert fill["action"] == "entry" and fill["total_cost"] > 0
    first = p.portfolio.iloc[0]
    assert first["ts"] == bars.index[K]
    # fresh capital: the equity at the first open was the initial capital
    assert p.trades.empty or p.trades.iloc[0]["entry_time"] == bars.index[K]
    assert first["cumulative_costs"] == pytest.approx(fill["total_cost"], abs=0)
    assert p.final_state.account.initial_capital == COSTS.initial_capital


# ── 3./4. transition at start-1 and INSUFFICIENT_DATA carried from the history ──


def test_transition_at_start_minus_1_is_decided_before_the_window() -> None:
    bars = ramp()
    states = CASES["transition_at_start_minus_1"][0]
    p = paper(bars, states, CONTROL, day(bars, K))
    ref = p.decisions.iloc[0]
    assert ref["strategy_state"] == L and ref["strategy_requested_target"] == 1.0
    assert p.fills.iloc[0]["executed_at"] == bars.index[K]


def test_insufficient_data_at_start_minus_1_keeps_the_request_from_the_history() -> None:
    bars = ramp()
    p = paper(bars, CASES["insufficient_at_start_minus_1"][0], CONTROL, day(bars, K))
    ref = p.decisions.iloc[0]
    assert ref["bar_ts"] == bars.index[K - 1] and ref["strategy_state"] == I
    assert ref["strategy_requested_target"] == 1.0  # carried from the LONG before, not 0
    assert p.fills.iloc[0]["executed_at"] == bars.index[K]


def test_insufficient_data_inside_the_window_keeps_the_request_from_before_it() -> None:
    """Probe case D: the former paper entered at bar 7 (first LONG close in the window)."""
    bars = ramp()
    p = paper(bars, CASES["insufficient_carries_long"][0], CONTROL, day(bars, K))
    window = p.decisions.iloc[1:3]
    assert window["strategy_state"].tolist() == [I, I]
    assert window["strategy_requested_target"].tolist() == [1.0, 1.0]
    assert p.fills.iloc[0]["executed_at"] == bars.index[K]
    assert len(p.fills) == 1  # held through the INSUFFICIENT_DATA sessions


def test_history_with_only_insufficient_data_requests_nothing() -> None:
    bars = ramp()
    p = paper(bars, [I] * K + [L] * 8, CONTROL, day(bars, K))
    assert p.decisions.iloc[0]["strategy_requested_target"] == 0.0
    assert p.fills.iloc[0]["executed_at"] == bars.index[K + 1]


# ── 5. causality ──


def test_reference_decision_reads_nothing_from_the_window() -> None:
    bars = ramp()
    states = [L] * 12
    base = paper(bars, states, ATR3, day(bars, K))
    other_bars = bars.copy()
    cols = ["open", "high", "low", "close"]
    other_bars[cols] = other_bars[cols].astype(float)
    other_bars.iloc[K:, :4] = other_bars.iloc[K:, :4] * 0.5  # window prices changed
    other = paper(other_bars, [*states[:K], *([F, I] * 4)], ATR3, day(bars, K))
    assert base.events[:2] == other.events[:2]  # decision + reference snapshot at start-1
    pd.testing.assert_series_equal(base.decisions.iloc[0], other.decisions.iloc[0])


def test_reference_decision_equals_one_made_without_the_window_data() -> None:
    bars = ramp()
    full = inputs(bars, [F, L, I, I] + [L] * 8)
    history, window = split_at_start(full, day(bars, K))
    assert [b.bar_ts for b in history] == list(bars.index[:K])
    assert [b.bar_ts for b in window] == list(bars.index[K:])
    engine = PaperTradingEngine(PaperConfig(risk=ATR3, backtest=FREE))
    a = engine.replay_inputs(full, strategy_id="x", start=day(bars, K))
    b = engine.replay_inputs(full[: K + 1], strategy_id="x", start=day(bars, K))
    assert a.events[:2] == b.events[:2]
    t = PaperTrader.new(
        PaperConfig(risk=ATR3, backtest=FREE), strategy_id="x", strategy_version="1.0.0"
    )
    step = t.prime(history)  # the history alone: no window session is ever passed
    assert step.decision.decision_id == a.decisions.iloc[0]["decision_id"]
    assert step.decision.risk_approved_target == a.decisions.iloc[0]["risk_approved_target"]
    assert t.state.pending == step.instruction


def test_reference_decision_does_not_update_the_risk_monitors() -> None:
    bars = ramp()
    history = inputs(bars, [L] * 12)[:K]
    t = PaperTrader.new(
        PaperConfig(risk=DD20, backtest=FREE), strategy_id="x", strategy_version="1"
    )
    hwm, state, last = t.state.risk.dd.hwm, t.state.risk.risk_state, t.state.risk.sess.last_equity
    step = t.prime(history)
    assert t.state.risk.dd.hwm == hwm and t.state.risk.risk_state == state
    assert t.state.risk.sess.last_equity == last
    assert t.state.session_seq == 0 and not t.state.account.holding
    assert step.order is None and step.fill is None and step.trade is None
    assert step.equity == FREE.initial_capital and step.exposure == 0.0
    assert step.snapshot.equity == FREE.initial_capital
    assert step.snapshot.position_quantity == 0.0 and step.snapshot.pending_kind == "entry"
    with pytest.raises(ValueError, match="only a new account"):
        t.prime(history)


# ── 6. boundary without a previous session ──


@pytest.mark.parametrize("start", [None, "first", "before"])
def test_no_previous_session_keeps_the_boundary_behaviour(start: str | None) -> None:
    bars = ramp()
    when = {None: None, "first": day(bars, 0), "before": date(2024, 1, 2)}[start]
    p = paper(bars, [L] * 12, ATR3, when)
    ref = paper(bars, [L] * 12, ATR3, None)
    assert p.decisions.iloc[0]["bar_ts"] == bars.index[0]
    assert p.events == ref.events
    assert p.fills.iloc[0]["executed_at"] == bars.index[1]
    assert_same_as_research(p, research(bars, [L] * 12, ATR3, 0))


def test_a_gap_just_before_the_window_fails_explicitly() -> None:
    bars = ramp()
    gapped = [b for i, b in enumerate(inputs(bars, [L] * 12)) if i != K - 1]
    with pytest.raises(ValueError, match="missing session"):
        split_at_start(gapped, day(bars, K))


# ── 7. stop and drawdown of the entry made at the window start ──


@pytest.mark.parametrize(
    ("risk", "bars_fn"), [(ATR3, stop_path_bars), (DD20, drawdown_path_bars)], ids=["stop", "dd"]
)
def test_stop_and_drawdown_after_a_window_start_match_the_research(
    risk: RiskConfig, bars_fn: Any
) -> None:
    bars = bars_fn()
    states = [L] * len(bars)
    k = 2
    p = paper(bars, states, risk, day(bars, k))
    r = research(bars, states, risk, k)
    assert_same_as_research(p, r)
    assert p.fills.iloc[0]["executed_at"] == bars.index[k]
    if risk is ATR3:
        assert r.decisions["stop_triggered"].any()  # the entry's own stop acts in the window
    else:
        assert (r.equity["risk_state"] == "LOCKED").any()


# ── results: the reference row is never a result session ──


def test_reference_row_is_not_in_results_returns_metrics_or_session_counts() -> None:
    bars = ramp()
    p = paper(bars, [L] * 12, ATR3, day(bars, K), bt=COSTS)
    window = list(bars.index[K:])
    assert list(p.equity.index) == window
    assert list(p.portfolio["ts"]) == window
    assert len(p.decisions) == len(window) + 1  # + the decision before the window
    np.testing.assert_array_equal(
        p.equity["daily_return"], returns_from_equity(p.equity["equity"], COSTS.initial_capital)
    )
    assert p.equity["daily_return"].iloc[0] == p.equity["equity"].iloc[0] / 100_000 - 1
    assert p.final_state.session_seq == len(window)
    snaps = [e for e in p.events if e["event_type"] == "snapshot"]
    assert len(snaps) == len(window) + 1 and snaps[0]["session"] == bars.index[K - 1].isoformat()
    assert [e["event_type"] for e in p.events[:2]] == ["decision", "snapshot"]
    full = paper(bars, [L] * 12, ATR3, None, bt=COSTS)
    assert p.metrics.keys() == full.metrics.keys()


# ── 8. incremental accounts ──


def store(root: Path, risk: RiskConfig = ATR3) -> PaperStore:
    return PaperStore(
        PaperConfig(risk=risk, backtest=FREE), strategy_id="x", strategy_version="1.0.0", root=root
    )


def test_new_incremental_account_with_start_equals_the_replay(tmp_path: Path) -> None:
    bars = ramp()
    states = [F, L, I, I] + [L] * 8
    ins = inputs(bars, states)
    ref = PaperTradingEngine(PaperConfig(risk=ATR3, backtest=FREE)).replay_inputs(
        ins, strategy_id="x", strategy_version="1.0.0", start=day(bars, K)
    )
    s = store(tmp_path)
    counts = s.process_many(ins[:6], start=day(bars, K))
    assert counts == {"processed": 2, "already_processed": 0, "recovered": 0}
    s = store(tmp_path)  # restart; `start` is ignored once the account exists
    counts = s.process_many(ins, start=day(bars, 0))
    assert counts == {"processed": 6, "already_processed": 6, "recovered": 0}
    assert s.events() == ref.events
    assert s.status()["sessions_processed"] == len(bars) - K
    rebuilt = reconstruct_snapshots(s.events(), FREE.initial_capital)
    assert rebuilt[0]["equity"] == FREE.initial_capital and rebuilt[0]["position_quantity"] == 0


def test_reference_decision_waits_until_the_session_before_start(tmp_path: Path) -> None:
    bars = ramp()
    ins = inputs(bars, [L] * 12)
    s = store(tmp_path)
    assert s.process_many(ins[: K - 1], start=day(bars, K))["processed"] == 0
    assert s.trader.state.last_session is None and s.events() == []
    s.process_many(ins[:K], start=day(bars, K))  # bar K-1 is now known; K not yet
    view = paper_svc.account_view(s)
    assert view["sessions_processed"] == 0 and view["equity"] == FREE.initial_capital
    assert view["pending_kind"] == "entry"
    assert view["next_effective_session"] == bars.index[K]
    assert [e["event_type"] for e in s.events()] == ["decision", "snapshot"]


def test_crash_after_the_reference_events_recovers(tmp_path: Path) -> None:
    bars = ramp()
    ins = inputs(bars, [L] * 12)
    s = store(tmp_path)
    state_before = s.state_path.read_text(encoding="utf-8")
    s.process_many(ins[:K], start=day(bars, K))
    s.state_path.write_text(state_before, encoding="utf-8")  # crash before the state write
    s = store(tmp_path)
    assert s.status()["uncommitted_ledger_events"] == 2
    s.process_many(ins, start=day(bars, K))
    assert s.status()["uncommitted_ledger_events"] == 0
    ref = PaperTradingEngine(PaperConfig(risk=ATR3, backtest=FREE)).replay_inputs(
        ins, strategy_id="x", strategy_version="1.0.0", start=day(bars, K)
    )
    assert s.events() == ref.events


def test_running_account_is_unaffected_by_start(tmp_path: Path) -> None:
    bars = ramp()
    ins = inputs(bars, [F, F, L, I] + [L] * 8)
    a, b = store(tmp_path / "a"), store(tmp_path / "b")
    a.process_many(ins[:3])
    b.process_many(ins[:3])
    a.process_many(ins, start=day(bars, 6))  # an existing account ignores `start`
    b.process_many(ins)
    assert a.events() == b.events()
    assert a.trader.state.session_seq == b.trader.state.session_seq == len(bars)


def test_dashboard_process_sessions_uses_the_window_start(tmp_path: Path) -> None:
    bars = ramp()
    ins = inputs(bars, [L] * 12)
    s = store(tmp_path)
    counts = paper_svc.paper_process_sessions(s, ins, start=day(bars, K))
    assert counts["processed"] == len(bars) - K
    fills = [e for e in s.events() if e["event_type"] == "fill"]
    assert fills[0]["session"] == bars.index[K].isoformat()
