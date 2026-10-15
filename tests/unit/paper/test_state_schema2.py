"""Paper state schema 2 (ADR-0034 D-A9): `risk.stop_activated` and `pending.exit_causes`.

Schema-1 documents are read under explicit rules, never by guessing; variants are built in
memory from the frozen fixtures (the fixture files are never modified). Synthetic data,
in-memory engines and `tmp_path` only.
"""

import copy
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.backtest.engine import session_times
from app.broker.config import BrokerConfig
from app.broker.reconcile import expected_quantity
from app.dashboard.services import paper as paper_svc
from app.paper.config import PaperConfig
from app.paper.engine import PaperTradingEngine, build_inputs
from app.paper.state import STATE_SCHEMA_VERSION, StateError, dumps, from_dict, loads
from app.paper.store import PaperStore
from app.risk.config import DrawdownSpec, RiskConfig
from tests.unit.alerts.test_rules import _paper_alerts
from tests.unit.risk.test_adr0034_baseline import ATR3, CONTROL, L
from tests.unit.risk.test_overlay import CAL, FREE, flat_indicators, make_bars

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "adr0034"


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return data


def v1_control() -> dict[str, Any]:
    return fixture("paper_state_v1_control.json")


CONTROL_CFG = PaperConfig(risk=CONTROL)


# ── schema-1 reading rules ──


def test_writes_are_schema_2() -> None:
    state, ptr = loads(json.dumps(v1_control()), CONTROL_CFG)
    d = json.loads(dumps(state, ptr))
    assert STATE_SCHEMA_VERSION == 2 and d["schema_version"] == 2
    assert d["risk"]["stop_activated"] is False and d["pending"]["exit_causes"] == ""


def test_schema_1_stop_profile_flat_loads_with_no_activation() -> None:
    d = fixture("paper_state_v1_atr_stop_3.json")
    cfg = PaperConfig(risk=ATR3)
    d["identity"]["config_fingerprint"] = cfg.fingerprint()  # isolate the schema-1 rule
    d["account"].update(shares=0.0, basis_notional=0.0, basis_costs=0.0)
    d["risk"]["stop"], d["open_trade"] = None, None
    state, _ = from_dict(d, cfg)
    assert state.risk.stop_activated is False


def test_schema_1_stop_profile_with_a_position_is_refused_whatever_the_fingerprint() -> None:
    d = fixture("paper_state_v1_atr_stop_3.json")
    cfg = PaperConfig(risk=ATR3)
    d["identity"]["config_fingerprint"] = cfg.fingerprint()
    with pytest.raises(StateError, match="never inferred"):
        from_dict(d, cfg)


def pending_exit(requested: float) -> dict[str, Any]:
    d = v1_control()
    d["pending"].update(pending_kind="exit", strategy_requested_target=requested,
                        risk_approved_target=0.0)  # fmt: skip
    return d


def test_schema_1_pending_exit_causes_are_derived_only_when_determined() -> None:
    state, _ = from_dict(pending_exit(0.0), CONTROL_CFG)
    assert state.pending is not None and state.pending.exit_causes == "strategy"
    state, _ = from_dict(pending_exit(1.0), CONTROL_CFG)  # no stop, no drawdown lock: sizing
    assert state.pending is not None and state.pending.exit_causes == "sizing"


def dd_variant(requested: float) -> tuple[dict[str, Any], PaperConfig]:
    cfg = PaperConfig(risk=RiskConfig("dd", drawdown=DrawdownSpec(enabled=True)))
    d = pending_exit(requested)
    d["identity"]["config_fingerprint"] = cfg.fingerprint()
    d["risk"]["drawdown"].update(state="LOCKED", lock_remaining=5)
    return d, cfg


def test_schema_1_pending_exit_under_a_drawdown_lock() -> None:
    d, cfg = dd_variant(0.0)
    state, _ = from_dict(d, cfg)
    assert state.pending is not None and state.pending.exit_causes == "drawdown_lock+strategy"
    d, cfg = dd_variant(1.0)  # drawdown lock and/or sizing: not determined
    with pytest.raises(StateError, match="cannot be determined"):
        from_dict(d, cfg)


# ── schema-2 validation ──


def v2_control() -> dict[str, Any]:
    state, ptr = loads(json.dumps(v1_control()), CONTROL_CFG)
    data: dict[str, Any] = json.loads(dumps(state, ptr))
    return data


@pytest.mark.parametrize(
    ("edit", "match"),
    [
        (lambda d: d["risk"].__setitem__("stop_activated", 1), "boolean"),
        (lambda d: d["risk"].__setitem__("stop_activated", True), "requires a stop profile"),
        (lambda d: d["risk"].pop("stop_activated"), "unexpected or missing"),
        (lambda d: d["pending"].__setitem__("exit_causes", "stop"), "empty unless"),
        (lambda d: d["pending"].update(pending_kind="exit", exit_causes=""), "empty on an exit"),
        (lambda d: d["pending"].update(pending_kind="exit", exit_causes="strategy+stop"),
         "not canonical"),
        (lambda d: d["pending"].update(pending_kind="exit", exit_causes="margin"), "unknown"),
        (lambda d: d["pending"].__setitem__("exit_causes", 3), "string"),
        (lambda d: d.__setitem__("schema_version", 3), "unsupported"),
        # not a version at all: an invalid (corrupt) state, not an incompatible one (audit fix 3)
        (lambda d: d.__setitem__("schema_version", True), "invalid state schema version"),
    ],
)  # fmt: skip
def test_schema_2_is_validated(edit: Any, match: str) -> None:
    d = v2_control()
    edit(d)
    with pytest.raises(StateError, match=match):
        from_dict(d, CONTROL_CFG)


def test_schema_2_round_trips_byte_for_byte() -> None:
    d = v2_control()
    state, ptr = from_dict(copy.deepcopy(d), CONTROL_CFG)
    assert json.loads(dumps(state, ptr)) == d


# ── an episode persisted across restarts (rejected exit) ──


def rejected_exit_inputs() -> list[Any]:
    rows: list[tuple[float, float, float, float]] = [(100.0, 101.0, 99.0, 100.0)] * 3
    full = make_bars(rows + [(99.0, 99.0, 92.0, 93.0)] + [(93.0, 94.0, 92.0, 93.0)] * 4)
    bars = full.drop(full.index[4])  # the exit's session is missing: the exit is rejected
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    return build_inputs(bars, pd.Series([L] * len(bars), index=bars.index),
                        flat_indicators(bars), times)  # fmt: skip


def store(root: Path) -> PaperStore:
    return PaperStore(PaperConfig(risk=ATR3, backtest=FREE), strategy_id="x",
                      strategy_version="1.0.0", root=root)  # fmt: skip


def test_episode_state_survives_restarts_and_equals_the_replay(tmp_path: Path) -> None:
    ins = rejected_exit_inputs()
    seen = []
    for i, bar in enumerate(ins):
        s = store(tmp_path)  # reopen before every session
        s.process(bar)
        seen.append(json.loads(s.state_path.read_text(encoding="utf-8"))["risk"]["stop_activated"])
        if i == 4:  # the exit was rejected at this open: the episode is still activated
            assert s.trader.state.risk.stop_activated and s.trader.state.account.holding
    assert seen == [False, False, False, True, True, False, False]
    ref = PaperTradingEngine(PaperConfig(risk=ATR3, backtest=FREE)).replay_inputs(
        ins, strategy_id="x", strategy_version="1.0.0"
    )
    assert store(tmp_path).events() == ref.events
    assert ref.trades.iloc[0]["exit_causes"] == "stop"


# ── ledger readers tolerate the additive decision fields ──


def test_ledger_readers_accept_the_new_fields(tmp_path: Path) -> None:
    ins = rejected_exit_inputs()
    s = store(tmp_path)
    s.process_many(ins[:5])  # holding, exit rejected, episode activated
    events = s.events()
    decision = next(e for e in events if e["event_type"] == "decision")
    assert {"stop_activation", "exit_causes"} <= set(decision["record"])
    qty, basis = expected_quantity(events, BrokerConfig())
    assert qty >= 0 and "decision" in basis
    tables = paper_svc.ledger_tables(s)
    assert "stop_activation" in tables["decision"].columns
    run = type("Run", (), {"events": events, "account_id": s.account_id})()
    assert _paper_alerts(run)  # type: ignore[no-untyped-call]


# ── a rejected stop exit, then FLAT above the level, then the confirmed exit (audit fix 1) ──


def rejected_then_flat_inputs() -> list[Any]:
    rows: list[tuple[float, float, float, float]] = [(100.0, 101.0, 99.0, 100.0)] * 3
    full = make_bars(
        rows
        + [(99.0, 99.0, 92.0, 93.0)]  # 3: close 93 <= stop 94 -> activation, exit for 4
        + [(93.0, 94.0, 92.0, 93.0)]  # 4: missing session -> the exit is rejected at 5
        + [(93.0, 96.0, 92.0, 95.0)]  # 5: close 95 > 94, the strategy turns FLAT
        + [(95.0, 96.0, 94.0, 95.0)] * 2  # 6: the exit is filled at the open
    )
    bars = full.drop(full.index[4])
    states = [L, L, L, L, "FLAT", "FLAT", "FLAT"]
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    return build_inputs(bars, pd.Series(states, index=bars.index), flat_indicators(bars), times)


def test_stop_stays_a_cause_until_the_exit_is_confirmed(tmp_path: Path) -> None:
    ins = rejected_then_flat_inputs()
    persisted = []
    for bar in ins:
        s = store(tmp_path)  # reopen before every session: the episode lives in state.json
        s.process(bar)
        d = json.loads(s.state_path.read_text(encoding="utf-8"))
        pending = d["pending"]
        persisted.append((d["risk"]["stop_activated"], pending and pending["exit_causes"]))
    assert persisted == [
        (False, ""),
        (False, ""),
        (False, ""),
        (True, "stop"),  # activation: exit scheduled
        (True, "stop+strategy"),  # exit rejected; FLAT above the level: the stop is kept
        (False, ""),  # exit filled: episode closed
        (False, ""),
    ]
    events = store(tmp_path).events()
    decisions = [e["record"] for e in events if e["event_type"] == "decision"]
    assert [x["stop_activation"] for x in decisions] == [False, False, False, True] + [False] * 3
    assert [x["exit_causes"] for x in decisions][3:5] == ["stop", "stop+strategy"]
    orders = [e["record"]["status"] for e in events if e["event_type"] == "order"]
    assert orders == ["FILLED", "REJECTED", "FILLED"]
    ref = PaperTradingEngine(PaperConfig(risk=ATR3, backtest=FREE)).replay_inputs(
        ins, strategy_id="x", strategy_version="1.0.0"
    )
    assert events == ref.events
    trade = ref.trades.iloc[0]
    assert trade["exit_causes"] == "stop+strategy"
    stops = [a for a in _paper_alerts(ref) if a.event_type == "RISK_STOP_TRIGGERED"]  # type: ignore[no-untyped-call]
    assert len(stops) == 1  # one activation, one alert
    assert ref.final_state.risk.stop_activated is False
