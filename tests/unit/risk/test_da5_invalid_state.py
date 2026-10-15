"""ADR-0034 D-A5 (step 4a): invalid financial state fails explicitly; equity zero is valid.

Complements the D-A5 specification tests of `test_adr0034_baseline.py` (monitors, both
engines) with the rules they do not cover: the undefined session loss keeps the protection,
no entry or increase at equity zero, an open position is kept, and an invalid state never
reaches a paper account's files. Synthetic data, in-memory engines and `tmp_path` only.
"""

import math
from pathlib import Path

import pandas as pd
import pytest

from app.backtest.engine import session_times
from app.paper.config import PaperConfig
from app.paper.engine import PaperTradingEngine
from app.paper.store import PaperStore
from app.risk.config import DrawdownSpec, RiskConfig, SessionSpec
from app.risk.limits import LOCKED, NORMAL, DrawdownMonitor, SessionMonitor, check_equity
from app.risk.manager import RiskManager
from tests.unit.paper.test_window_start import inputs, ramp
from tests.unit.risk.test_adr0034_baseline import CONTROL, DD20, ENTRY, L, zero_close_bars
from tests.unit.risk.test_overlay import CAL, FREE, flat_indicators, make_bars, run

INVALID = [-1e-9, -5.0, math.nan, math.inf, -math.inf]


@pytest.mark.parametrize("equity", INVALID)
def test_invalid_equity_is_refused(equity: float) -> None:
    with pytest.raises(ValueError, match="invalid equity"):
        check_equity(equity)


@pytest.mark.parametrize("equity", [0.0, 1e-300, 100.0])
def test_zero_and_positive_equity_are_valid(equity: float) -> None:
    check_equity(equity)


# ── session monitor ──


@pytest.mark.parametrize("equity", INVALID)
def test_session_monitor_refuses_invalid_equity(equity: float) -> None:
    m = SessionMonitor(SessionSpec(enabled=True), last_equity=100.0)
    with pytest.raises(ValueError):
        m.update(equity)


def test_undefined_session_loss_does_not_shorten_an_active_lock() -> None:
    m = SessionMonitor(SessionSpec(enabled=True, lock_sessions=3), last_equity=100.0)
    assert m.update(90.0) == pytest.approx(-0.1)  # breach: locked for 3 decisions
    assert m.lock_remaining == 3
    m.update(0.0)  # reaching 0 is a defined loss of -100%: still a breach
    assert m.lock_remaining == 3
    assert m.update(0.0) is None  # previous equity 0: not computable
    assert m.lock_remaining == 3 and m.locked  # neither released nor shortened


def test_undefined_session_loss_blocks_increases_then_computation_resumes() -> None:
    m = SessionMonitor(SessionSpec(enabled=True), last_equity=0.0)
    assert m.update(10.0) is None and m.locked
    assert m.update(10.0) == 0.0  # previous equity 10 again: defined, the lock counts down
    assert not m.locked


def test_disabled_session_monitor_reports_undefined_loss_without_locking() -> None:
    m = SessionMonitor(SessionSpec(enabled=False), last_equity=0.0)
    assert m.update(10.0) is None and not m.locked


def test_session_monitor_finite_values_are_unchanged() -> None:
    m = SessionMonitor(SessionSpec(enabled=True, max_session_loss=0.05), last_equity=100.0)
    assert m.update(96.0) == 96.0 / 100.0 - 1 and not m.locked
    assert m.update(90.0) == 90.0 / 96.0 - 1 and m.locked


# ── no new entry or increase at equity zero; an open position is kept ──


def decide(rm: RiskManager, *, requested: float, equity: float):  # type: ignore[no-untyped-def]
    return rm.decide(
        requested=requested,
        close=100.0,
        realized_vol=0.2,
        atr=2.0,
        equity=equity,
        triggered=False,
        touch=False,
        strategy_state="LONG" if requested > 0 else "FLAT",
    )


def test_no_entry_at_zero_equity() -> None:
    rm = RiskManager(CONTROL, 100_000.0)
    d = decide(rm, requested=1.0, equity=0.0)
    assert d.approved_exposure == 0.0 and d.intervention
    assert "zero_equity_no_increase" in d.reasons


def test_open_position_is_kept_at_zero_equity() -> None:
    rm = RiskManager(CONTROL, 100_000.0)
    rm.current_target = 0.5  # an executed position
    d = decide(rm, requested=1.0, equity=0.0)  # `full` sizing asks for 1.0
    assert d.approved_exposure == 0.5  # not raised, not forced out
    assert d.reasons == ("zero_equity_no_increase",)
    rm.current_target = 1.0  # fully invested: nothing to increase
    held = decide(rm, requested=1.0, equity=0.0)
    assert held.approved_exposure == 1.0 and held.reasons == ()
    out = decide(rm, requested=0.0, equity=0.0)  # an exit stays possible
    assert out.approved_exposure == 0.0 and out.reasons == ()


def test_positive_equity_decisions_are_unchanged() -> None:
    a = decide(RiskManager(CONTROL, 100_000.0), requested=1.0, equity=1e-9)
    assert a.approved_exposure == 1.0 and a.reasons == ()


def test_research_zero_equity_close_keeps_the_lock_and_no_reentry() -> None:
    r = run(zero_close_bars(0.0), [L] * 4, DD20)
    last = r.decisions.iloc[-1]
    assert last["approved_exposure"] == 0.0 and last["risk_state"] == "LOCKED"
    assert r.equity["position"].iloc[-1] == 0.0


@pytest.mark.parametrize("risk", [CONTROL, DD20], ids=lambda r: r.name)
def test_research_nan_close_while_flat_fails_too(risk: RiskConfig) -> None:
    bars = zero_close_bars(math.nan)
    with pytest.raises(ValueError, match="invalid equity"):
        run(bars, ["FLAT"] * 4, risk)  # flat: 0 shares x NaN close is still NaN equity


# ── paper: an invalid state never reaches the account files ──


def test_paper_store_fails_closed_on_an_invalid_close(tmp_path: Path) -> None:
    bars = ramp(6)
    bars.loc[bars.index[4], ["close", "low"]] = math.nan
    ins = inputs(bars, [L] * 6)
    s = PaperStore(
        PaperConfig(risk=DD20, backtest=FREE), strategy_id="x", strategy_version="1.0.0",
        root=tmp_path,
    )  # fmt: skip
    s.process_many(ins[:4])
    before = (s.ledger_path.read_bytes(), s.state_path.read_bytes())
    with pytest.raises(ValueError, match="invalid equity"):
        s.process(ins[4])
    assert (s.ledger_path.read_bytes(), s.state_path.read_bytes()) == before
    with pytest.raises(RuntimeError, match="reopen"):
        s.process(ins[4])  # the store object is broken until reopened


# ── end of the drawdown cooldown at equity zero (D-A5 x D-A6) ──

BLOCK3 = RiskConfig(
    "dd_block_entries_cd3",
    drawdown=DrawdownSpec(enabled=True, action="block_entries", cooldown_sessions=3),
)


def total_loss_bars() -> pd.DataFrame:
    """Invested at 100 (FREE costs), then every close at 0 while the position is kept."""
    return make_bars([*ENTRY, (95, 96, 89, 90)] + [(90, 91, 0.0, 0.0)] * 8)


def test_cooldown_ending_at_zero_equity_keeps_the_positive_hwm_and_relocks() -> None:
    m = DrawdownMonitor(DrawdownSpec(enabled=True, cooldown_sessions=2), hwm=100.0)
    assert m.update(0.0) == -1.0 and m.state == LOCKED and m.lock_remaining == 2
    m.update(0.0)
    assert m.lock_remaining == 1
    dd = m.update(0.0)  # end of the cooldown at equity 0
    assert m.hwm == 100.0  # never reset to 0
    assert dd == -1.0 and m.state == LOCKED and m.lock_remaining == 2  # a new lock
    assert m.update(0.0) == -1.0  # the next close: no ValueError from a zero HWM
    assert m.state == LOCKED and m.max_drawdown == -1.0


def test_cooldown_ending_at_positive_equity_is_unchanged() -> None:
    m = DrawdownMonitor(DrawdownSpec(enabled=True, cooldown_sessions=2), hwm=100.0)
    m.update(0.0)
    m.update(0.0)
    assert m.update(5.0) == 0.0  # positive equity at the end: unlock, HWM reset to 5
    assert m.hwm == 5.0 and m.state == NORMAL and m.lock_remaining == 0
    m2 = DrawdownMonitor(DrawdownSpec(enabled=True, cooldown_sessions=2), hwm=100.0)
    m2.update(70.0)
    m2.update(60.0)
    assert m2.update(65.0) == 0.0 and m2.hwm == 65.0 and m2.state == NORMAL


def test_recovery_after_a_zero_equity_relock_follows_the_existing_rule() -> None:
    m = DrawdownMonitor(DrawdownSpec(enabled=True, cooldown_sessions=1), hwm=100.0)
    m.update(0.0)  # lock (1 decision)
    m.update(0.0)  # end at 0: relocked, HWM 100 kept
    assert m.hwm == 100.0 and m.state == LOCKED
    assert m.update(10.0) == 0.0  # end of that cooldown at positive equity: normal unlock
    assert m.hwm == 10.0 and m.state == NORMAL


@pytest.mark.parametrize("action", ["force_flat", "block_entries"])
def test_no_entry_or_increase_after_a_cooldown_ending_at_zero_equity(action: str) -> None:
    risk = RiskConfig("dd", drawdown=DrawdownSpec(enabled=True, action=action, cooldown_sessions=2))
    for current in (0.0, 0.5):
        rm = RiskManager(risk, 100_000.0)
        rm.current_target = current
        for _ in range(4):  # lock, cooldown end at 0 (relock), one more close
            rm.on_close(0.0, low=0.0, close=0.0, holding=current > 0)
        assert rm.dd.hwm == 100_000.0 and rm.risk_state == LOCKED
        d = decide(rm, requested=1.0, equity=0.0)
        assert d.approved_exposure <= current
        assert d.risk_state == LOCKED and d.drawdown == -1.0


@pytest.mark.parametrize("engine", ["research", "paper"])
def test_engines_keep_the_lock_through_a_cooldown_at_zero_equity(engine: str) -> None:
    bars = total_loss_bars()
    states = [L] * len(bars)
    if engine == "research":
        r = run(bars, states, BLOCK3)
        frame, approved = r.equity, r.decisions["approved_exposure"]
        assert r.diagnostics["drawdown_lock_events"] == 1.0  # one episode, not one per relock
    else:
        p = PaperTradingEngine(PaperConfig(risk=BLOCK3, backtest=FREE)).replay(
            bars,
            pd.Series(states, index=bars.index),
            flat_indicators(bars),
            session_times(pd.DatetimeIndex(bars.index), CAL),
            strategy_id="x",
        )
        frame, approved = p.portfolio, p.decisions["risk_approved_target"]
    zero = frame["equity"] == 0.0
    assert zero.sum() == 8 and (frame.loc[zero, "risk_state"] == LOCKED).all()
    assert (approved <= 1.0).all()  # the position is kept, never increased
    if engine == "research":
        assert (frame.loc[zero, "drawdown"] == -1.0).all()  # the total loss stays visible
