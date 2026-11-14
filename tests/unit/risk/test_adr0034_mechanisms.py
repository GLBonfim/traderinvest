"""ADR-0034 mechanisms (implemented): stop episodes and exit causes (D-A1), drawdown level
comparison (D-A2), ATR stop inputs (D-A3), explicit `strategy_state` (D-A4), separate
counters (D-A8) and per-mechanism semantic versions.

Synthetic bars, in-memory engines and pure functions only (no database, account or network).
"""

import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import session_times
from app.paper.config import MODE, PAPER_VERSION, PaperConfig
from app.paper.engine import PaperTradingEngine
from app.risk import artifacts as art
from app.risk.config import (
    DRAWDOWN_SEMANTICS,
    RISK_VERSION,
    SCENARIOS,
    SESSION_SEMANTICS,
    STOP_SEMANTICS,
    DrawdownSpec,
    RiskConfig,
    SessionSpec,
    SizingSpec,
    StopSpec,
)
from app.risk.limits import LOCKED, NORMAL, WARNING, DrawdownMonitor
from app.risk.manager import (
    EXIT_CAUSES,
    RiskManager,
    canonical_exit_causes,
    validate_exit_causes,
)
from tests.unit.alerts.test_rules import _paper_alerts
from tests.unit.risk.test_adr0034_baseline import (
    ATR3,
    CONTROL,
    DD20,
    ENTRY,
    F,
    I,
    L,
    coincident_bars,
    drawdown_path_bars,
    paper_rejected_exit_run,
    stop_path_bars,
    threshold_bars,
)
from tests.unit.risk.test_overlay import CAL, FREE, flat_indicators, make_bars, run
from tests.unit.risk.test_research_artifacts import (
    _fixed_code,  # noqa: F401 (autouse fixture: fixed code version in manifests)
    manifest,
    run_grid,
)

ATR_RISK = next(s for s in SCENARIOS if s.name == "atr_risk_1pct")


def paper(bars: pd.DataFrame, states: list[str], risk: RiskConfig, ind: Any = None) -> Any:
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    return PaperTradingEngine(PaperConfig(risk=risk, backtest=FREE)).replay(
        bars,
        pd.Series(states, index=bars.index),
        ind if ind is not None else flat_indicators(bars),
        times,
        strategy_id="x",
    )


def decide(rm: RiskManager, **kw: Any) -> Any:
    args = {"requested": 1.0, "close": 100.0, "realized_vol": 0.2, "atr": 2.0,
            "equity": 10_000.0, "triggered": False, "touch": False,
            "strategy_state": L}  # fmt: skip
    return rm.decide(**{**args, **kw})


# ── D-A1: exit causes ──


def test_exit_causes_are_canonical_and_validated() -> None:
    assert canonical_exit_causes(["strategy", "stop", "stop"]) == "stop+strategy"
    assert canonical_exit_causes({"stop", "drawdown_lock"}) == "drawdown_lock+stop"
    assert set(EXIT_CAUSES) == {"drawdown_lock", "session_lock", "sizing", "stop", "strategy"}
    validate_exit_causes("drawdown_lock+stop+strategy")
    for bad in ("", "stop+stop", "strategy+stop", "margin", "stop+", "Stop"):
        with pytest.raises(ValueError):
            validate_exit_causes(bad)
    with pytest.raises(ValueError, match="unknown"):
        canonical_exit_causes(["margin"])


def two_episode_bars() -> pd.DataFrame:
    """Entry at 100 (stop 94), close 93 triggers; FLAT; re-entry at 95 (stop 89); 88 triggers."""
    return make_bars([*ENTRY, (99, 99, 92, 93), (93, 94, 92, 93), (94, 95, 93, 95),
                      (95, 96, 94, 95), (94, 94, 87, 88), (88, 89, 87, 88)])  # fmt: skip


TWO_EPISODE_STATES = [L, L, L, F, L, L, L, L]


@pytest.mark.parametrize(
    ("bars_fn", "states", "risk", "causes", "reason"),
    [
        (coincident_bars, [L, L, F, F], ATR3, "stop+strategy", "strategy_or_sizing"),
        (stop_path_bars, [L] * 6, ATR3, "stop", "stop_triggered"),
        (lambda: make_bars([(100, 101, 99, 100)] * 5), [L, L, F, F, F], CONTROL, "strategy",
         "strategy_or_sizing"),
        (drawdown_path_bars, [L] * 43, DD20, "drawdown_lock", "drawdown_lock_flat"),
    ],
    ids=["coincident", "stop", "strategy", "drawdown_lock"],
)  # fmt: skip
def test_trade_exit_causes_and_unchanged_exit_reason(
    bars_fn: Any, states: list[str], risk: RiskConfig, causes: str, reason: str
) -> None:
    bars = bars_fn()
    r = run(bars, states, risk)
    first = r.trades.iloc[0]
    assert first["exit_causes"] == causes
    assert first["exit_reason"] == reason  # unchanged meaning: the risk decision reason
    p = paper(bars, states, risk)
    assert p.trades.iloc[0]["exit_causes"] == causes  # same attribution in paper


def test_coincident_stop_and_strategy_exit_is_one_activation_and_one_exit() -> None:
    bars = coincident_bars()
    r = run(bars, [L, L, F, F], ATR3)
    d = r.decisions
    assert d["stop_activation"].tolist() == [False, False, True, False]
    assert d.iloc[2]["reasons"] == ""  # the risk decision reason is unchanged (none)
    assert d.iloc[2]["exit_causes"] == "stop+strategy"
    assert r.fills["side"].tolist() == ["buy", "sell"]  # one exit at the next open
    assert r.diagnostics["stop_activations"] == 1.0 and r.diagnostics["stop_triggers"] == 1.0


def test_sizing_exit_cause_when_the_risk_input_becomes_undefined() -> None:
    bars = make_bars([(100, 101, 99, 100)] * 6)
    ind = flat_indicators(bars)
    ind.loc[ind.index[3], "atr_14"] = math.nan  # atr_risk sizing cannot size at bar 3
    r = run(bars, [L] * 6, ATR_RISK, ind)
    assert "risk_input_undefined" in r.decisions.iloc[3]["reasons"]
    assert r.trades.iloc[0]["exit_causes"] == "sizing"


def test_stop_and_drawdown_lock_on_the_same_close_are_both_causes() -> None:
    both = RiskConfig("both", stop=StopSpec("atr"), drawdown=DrawdownSpec(enabled=True))
    bars = make_bars([*ENTRY, (99, 99, 78, 79), (79, 80, 78, 79)])  # 79 <= stop 94 and <= 80%
    r = run(bars, [L] * 4, both)
    assert r.decisions.iloc[2]["exit_causes"] == "drawdown_lock+stop"
    assert r.trades.iloc[0]["exit_causes"] == "drawdown_lock+stop"


def test_each_position_episode_activates_once() -> None:
    r = run(two_episode_bars(), TWO_EPISODE_STATES, ATR3)
    expected = [False, False, True, False, False, False, True, False]  # bars 2 and 6
    assert r.decisions["stop_activation"].tolist() == expected
    assert r.diagnostics["stop_activations"] == 2.0
    assert r.trades["exit_causes"].tolist() == ["stop", "stop"]


def test_lockout_keeps_blocking_without_new_activations() -> None:
    rows: list[tuple[float, float, float, float]] = [*ENTRY, (99, 99, 92, 93)]
    bars = make_bars(rows + [(94.0, 95.0, 93.0, 94.0)] * 3)
    r = run(bars, [L, L, L, I, L, L], ATR3)
    reasons = r.decisions["reasons"].tolist()
    assert "stop_lockout_until_signal_reset" in reasons[3] and reasons[4] and reasons[5]
    assert r.decisions["stop_activation"].sum() == 1


def test_paper_rejected_exit_keeps_the_episode_without_new_activations() -> None:
    p = paper_rejected_exit_run()
    d = p.decisions
    triggered = d["intervention_reason"] == "stop_triggered"
    assert triggered.sum() == 2  # the condition repeats while the exit is not executed
    assert d.loc[triggered, "stop_activation"].tolist() == [True, False]  # one activation
    assert p.trades.iloc[0]["exit_causes"] == "stop"
    assert p.final_state.risk.stop_activated is False  # reset by the confirmed exit fill
    stops = [
        a
        for a in _paper_alerts(p)  # type: ignore[no-untyped-call]
        if a.event_type == "RISK_STOP_TRIGGERED"
    ]
    assert len(stops) == 1


def test_alerts_on_records_without_stop_activation_keep_the_former_rule() -> None:
    p = paper_rejected_exit_run()
    old = [
        {**e, "record": {k: v for k, v in e["record"].items() if k != "stop_activation"}}
        if e["event_type"] == "decision"
        else e
        for e in p.events
    ]
    p_old = type("Run", (), {"events": old, "account_id": p.account_id})()
    stops = [
        a
        for a in _paper_alerts(p_old)  # type: ignore[no-untyped-call]
        if a.event_type == "RISK_STOP_TRIGGERED"
    ]
    assert len(stops) == 2  # history written before ADR-0034: unchanged alert count


def test_zero_equity_reason_is_not_a_stop_or_an_exit_cause() -> None:
    rm = RiskManager(ATR3, 10_000.0)
    d = decide(rm, equity=0.0)
    assert d.reasons == ("zero_equity_no_increase",)
    assert d.stop_activation is False and d.exit_causes == "" and not rm.stop_activated


def test_paper_matches_research_on_d_a1_fields() -> None:
    bars = two_episode_bars()
    r, p = run(bars, TWO_EPISODE_STATES, ATR3), paper(bars, TWO_EPISODE_STATES, ATR3)
    assert p.decisions["stop_activation"].tolist() == r.decisions["stop_activation"].tolist()
    assert p.decisions["exit_causes"].tolist() == r.decisions["exit_causes"].tolist()
    assert p.trades["exit_causes"].tolist() == r.trades["exit_causes"].tolist()


# ── D-A2: direct comparison with the level ──


@pytest.mark.parametrize("hwm", [100.0, 10_000.0, 123_456.789, 1e-3, 0.1 + 0.2])
@pytest.mark.parametrize(("threshold", "state"), [("lock", LOCKED), ("warning", WARNING)])
def test_level_boundary(hwm: float, threshold: str, state: str) -> None:
    spec = DrawdownSpec(enabled=True)
    level = hwm * (1 - getattr(spec, threshold))
    for equity, expected in [
        (level, state),
        (np.nextafter(level, -math.inf), LOCKED if threshold == "lock" else state),
        (np.nextafter(level, math.inf), WARNING if threshold == "lock" else NORMAL),
    ]:
        m = DrawdownMonitor(spec, hwm=hwm)
        m.update(float(equity))
        assert m.state == expected, (hwm, threshold, equity)


def test_reported_drawdown_is_still_the_ratio() -> None:
    m = DrawdownMonitor(DrawdownSpec(enabled=True), hwm=100.0)
    assert m.update(80.0) == 80.0 / 100.0 - 1 and m.state == LOCKED


def test_zero_equity_locks_at_the_level() -> None:
    m = DrawdownMonitor(DrawdownSpec(enabled=True), hwm=100.0)
    m.update(0.0)
    assert m.state == LOCKED and m.lock_level == 100.0 * (1 - 0.2)


def test_paper_matches_research_on_the_boundary_path() -> None:
    bars = threshold_bars()
    r, p = run(bars, [L] * 6, DD20), paper(bars, [L] * 6, DD20)
    assert p.portfolio["risk_state"].tolist() == r.equity["risk_state"].tolist()
    assert r.equity["risk_state"].iloc[3] == LOCKED


# ── D-A3: ATR stop inputs ──


@pytest.mark.parametrize("atr", [math.nan, math.inf, -math.inf, 0.0, -1.0])
def test_entry_without_a_valid_atr_is_refused(atr: float) -> None:
    rm = RiskManager(ATR3, 10_000.0)
    d = decide(rm, atr=atr)
    assert d.approved_exposure == 0.0 and d.reasons[-1] == "stop_input_undefined"
    assert d.exit_causes == ""  # nothing held: not an exit


@pytest.mark.parametrize("atr", [math.nan, 0.0])
def test_open_position_is_not_liquidated_by_an_invalid_atr(atr: float) -> None:
    rm = RiskManager(ATR3, 10_000.0)
    rm.current_target = 1.0
    d = decide(rm, atr=atr)
    assert d.approved_exposure == 1.0 and "stop_input_undefined" not in d.reasons


@pytest.mark.parametrize("method", ["fixed_pct", "none"])
def test_fixed_pct_and_no_stop_need_no_atr(method: str) -> None:
    rm = RiskManager(RiskConfig("p", stop=StopSpec(method)), 10_000.0)
    d = decide(rm, atr=math.nan)
    assert d.approved_exposure == 1.0 and d.reasons == ()


def test_engines_enter_once_the_atr_is_defined() -> None:
    bars = make_bars([(100, 101, 99, 100)] * 6)
    ind = flat_indicators(bars)
    ind.loc[ind.index[:3], "atr_14"] = math.nan
    r, p = run(bars, [L] * 6, ATR3, ind), paper(bars, [L] * 6, ATR3, ind)
    assert r.decisions["reasons"].tolist()[:3] == ["stop_input_undefined"] * 3
    assert r.fills.iloc[0]["ts"] == bars.index[4]  # decided at bar 3 (first valid ATR)
    assert p.fills.iloc[0]["executed_at"] == bars.index[4]
    assert r.decisions.iloc[4]["stop_level"] == 100.0 - 3 * 2.0


# ── D-A4: explicit strategy_state ──


def test_strategy_state_is_required_and_keyword_only() -> None:
    rm = RiskManager(ATR3, 10_000.0)
    args: dict[str, Any] = {"requested": 1.0, "close": 100.0, "realized_vol": 0.2, "atr": 2.0,
                            "equity": 10_000.0, "triggered": False, "touch": False}  # fmt: skip
    with pytest.raises(TypeError):
        rm.decide(**args)
    with pytest.raises(ValueError, match="unknown strategy state"):
        rm.decide(**args, strategy_state="SHORT")
    with pytest.raises(ValueError, match="contradicts"):
        rm.decide(**{**args, "requested": 0.0}, strategy_state=L)
    with pytest.raises(ValueError, match="contradicts"):
        rm.decide(**args, strategy_state=F)


def test_only_flat_releases_the_lockout() -> None:
    rm = RiskManager(ATR3, 10_000.0)
    rm.stop_lockout = True
    decide(rm, requested=0.0, strategy_state=I)  # a 0 request carried by INSUFFICIENT_DATA
    assert rm.stop_lockout  # not released (before ADR-0034 D-A4 a 0 request released it)
    decide(rm, requested=1.0, strategy_state=I)
    assert rm.stop_lockout
    decide(rm, requested=0.0, strategy_state=F)
    assert not rm.stop_lockout


# ── D-A8: separate counters ──


def test_a_persistent_lock_is_one_drawdown_event() -> None:
    r = run(drawdown_path_bars(), [L] * 43, DD20)
    assert r.diagnostics["drawdown_lock_events"] == 1.0
    assert (r.equity["drawdown_state"] == LOCKED).sum() == 21
    assert r.diagnostics["stop_activations"] == 0.0 and r.diagnostics["session_lock_events"] == 0.0


def test_a_relock_after_an_unlock_is_a_new_drawdown_event() -> None:
    risk = RiskConfig("dd_cd2", drawdown=DrawdownSpec(enabled=True, cooldown_sessions=2))
    path = [100, 100, 79, 79, 79, 79, 60, 60, 60]  # lock, unlock (HWM 79), fall again to 60
    r = run(make_bars([(p, p + 0.5, p - 0.5, p) for p in path]), [L] * 9, risk)
    assert r.diagnostics["drawdown_lock_events"] == 2.0


def test_a_session_lock_is_not_a_drawdown_event() -> None:
    risk = RiskConfig("sess", session=SessionSpec(enabled=True, max_session_loss=0.03))
    r = run(make_bars([*ENTRY, (100, 100, 94, 95), (95, 96, 94, 95)]), [L] * 4, risk)
    assert r.diagnostics["session_lock_events"] == 1.0
    assert r.diagnostics["drawdown_lock_events"] == 0.0
    assert LOCKED in r.equity["risk_state"].tolist()  # the combined state shows the lock
    assert (r.equity["drawdown_state"] == NORMAL).all()


def test_monitor_columns_are_appended_after_the_existing_ones() -> None:
    eq = run(drawdown_path_bars(), [L] * 43, DD20).equity
    assert list(eq.columns)[-2:] == ["drawdown_state", "session_locked"]


def test_two_episodes_count_two_conditions_and_two_activations() -> None:
    r = run(two_episode_bars(), TWO_EPISODE_STATES, ATR3)
    assert r.diagnostics["stop_triggers"] == 2.0 and r.diagnostics["stop_activations"] == 2.0


# ── semantic versions and fingerprints ──


def pre_adr_risk_fingerprint(risk: RiskConfig) -> str:
    import hashlib

    raw = json.dumps({"risk": RISK_VERSION, **asdict(risk)}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def pre_adr_paper_fingerprint(cfg: PaperConfig) -> str:
    import hashlib

    body = {"paper": PAPER_VERSION, "mode": MODE, **asdict(cfg)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]


def test_semantic_versions_follow_the_enabled_mechanisms() -> None:
    assert (STOP_SEMANTICS, DRAWDOWN_SEMANTICS, SESSION_SEMANTICS) == ("2.0.0", "2.0.0", "1.0.0")
    assert RISK_VERSION == "1.0.0"
    got = {s.name: s.semantics() for s in SCENARIOS}
    assert got == {
        "control_no_overlay": {},
        "fixed_fraction_50": {},
        "volatility_target_10": {},
        "drawdown_lock_20": {"drawdown": "2.0.0"},
        "atr_stop_3": {"stop": "2.0.0"},
        "atr_risk_1pct": {},
    }
    all3 = RiskConfig("all", stop=StopSpec("fixed_pct"), drawdown=DrawdownSpec(enabled=True),
                      session=SessionSpec(enabled=True))  # fmt: skip
    assert all3.semantics() == {"stop": "2.0.0", "drawdown": "2.0.0", "session": "1.0.0"}


@pytest.mark.parametrize("risk", SCENARIOS, ids=lambda r: r.name)
def test_payload_changes_only_with_an_enabled_mechanism(risk: RiskConfig) -> None:
    unchanged = not risk.semantics()
    assert (risk.fingerprint() == pre_adr_risk_fingerprint(risk)) == unchanged
    cfg = PaperConfig(risk=risk)
    assert (cfg.fingerprint() == pre_adr_paper_fingerprint(cfg)) == unchanged


def test_sizing_only_profiles_have_no_semantic_version() -> None:
    sized = RiskConfig("s", sizing=SizingSpec("atr_risk", atr_risk_budget=0.01))
    assert sized.semantics() == {} and sized.fingerprint() == pre_adr_risk_fingerprint(sized)


def test_research_configuration_records_the_semantic_versions(tmp_path: Path) -> None:
    rc, out = run_grid(tmp_path)  # fake suite: control_no_overlay and atr_stop_3
    assert rc == 0 and art.verify_run(out).ok
    scen = manifest(out)["config"]["risk_scenarios"]
    assert scen["control_no_overlay"]["semantics"] == {}
    assert scen["atr_stop_3"]["semantics"] == {"stop": STOP_SEMANTICS}
    assert scen["atr_stop_3"]["fingerprint"] == ATR3.fingerprint()
