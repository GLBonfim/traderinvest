"""T4: `derive_intent` on synthetic paper accounts whose ledger and state are schema 2
(ADR-0034). Expectations follow the function's existing contract only: it reads the account
through `read_paper_account`, uses the LAST decision and the LAST snapshot, and returns an
Intent for a pending entry / exit / rebalance not yet due; it raises for an unreadable
account. It does not consume `stop_activation` or `exit_causes` (they must not disturb it).
Temporary directories only; no gateway, no network.
"""

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.backtest.engine import session_times
from app.broker.config import OPG_WHOLE_SHARES, BrokerConfig
from app.broker.executor import derive_intent, target_quantity
from app.dashboard.services import paper as paper_svc
from app.paper.engine import build_inputs
from app.paper.store import PaperStore
from tests.unit.risk.test_overlay import CAL, flat_indicators, make_bars

CFG = BrokerConfig()
Row = tuple[float, float, float, float]


def account(root: Path, scenario: str, rows: list[Row], states: list[str]) -> PaperStore:
    bars = make_bars(rows)
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    st = paper_svc.paper_create_account(
        root, strategy_id="sma_trend", strategy_version="1.0.0", scenario_name=scenario,
        instrument="SPY",
    )  # fmt: skip
    st.process_many(build_inputs(bars, pd.Series(states, index=bars.index),
                                 flat_indicators(bars), times))  # fmt: skip
    return st


def last(st: PaperStore, kind: str) -> dict[str, Any]:
    record: dict[str, Any] = [e["record"] for e in st.events() if e["event_type"] == kind][-1]
    return record


def before_due(st: PaperStore) -> datetime:
    """A time after the last decision's close and before its scheduled open."""
    obs = pd.Timestamp(last(st, "decision")["observed_at"]).to_pydatetime()
    return obs + timedelta(hours=1)


def digest(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir())}


FLAT_THEN_LONG: list[Row] = [(500.0, 501.0, 499.0, 500.0)] * 6
STOP_PATH: list[Row] = [(100.0, 101.0, 99.0, 100.0)] * 3 + [(99.0, 99.0, 92.0, 93.0)]


def test_pending_entry_intent(tmp_path: Path) -> None:
    st = account(tmp_path, "control_no_overlay", FLAT_THEN_LONG, ["FLAT"] * 5 + ["LONG"])
    d, s = last(st, "decision"), last(st, "snapshot")
    assert d["stop_activation"] is False and d["exit_causes"] == ""  # schema-2 fields present
    before = digest(st.dir)
    intent = derive_intent(st.dir, CFG, before_due(st))
    assert intent is not None
    assert (intent.pending_kind, intent.approved_exposure) == ("entry", 1.0)
    assert intent.reference_price == s["mark_price"] == 500.0
    assert intent.decision_id == d["decision_id"]
    assert intent.client_order_id == f"qp-{st.account_id}-{d['decision_id'].split('-')[-1]}"
    assert intent.scheduled_for == d["scheduled_execution_at"]
    assert intent.target_qty == target_quantity(1.0, CFG.sizing_capital, 500.0, CFG.order_policy)
    assert digest(st.dir) == before  # read-only


def test_pending_stop_exit_intent(tmp_path: Path) -> None:
    st = account(tmp_path, "atr_stop_3", STOP_PATH, ["LONG"] * 4)
    d = last(st, "decision")
    assert d["pending_kind"] == "exit" and d["exit_causes"] == "stop" and d["stop_activation"]
    state = json.loads(st.state_path.read_text(encoding="utf-8"))
    assert state["schema_version"] == 2 and state["risk"]["stop_activated"] is True
    assert state["pending"]["exit_causes"] == "stop"
    intent = derive_intent(st.dir, CFG, before_due(st))
    assert intent is not None
    assert (intent.pending_kind, intent.approved_exposure, intent.target_qty) == ("exit", 0.0, 0.0)
    assert intent.reference_price == 93.0


def test_pending_stop_plus_strategy_exit_after_a_rejected_exit(tmp_path: Path) -> None:
    """The pending exit re-decided after a rejected stop exit carries `stop+strategy`; the
    intent is the same kind of exit (the causes are not part of the intent)."""
    rows = [*STOP_PATH, (93.0, 94.0, 92.0, 93.0), (93.0, 96.0, 92.0, 95.0)]
    full = make_bars(rows)
    bars = full.drop(full.index[4])  # the exit's session is missing
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    st = paper_svc.paper_create_account(
        tmp_path, strategy_id="sma_trend", strategy_version="1.0.0", scenario_name="atr_stop_3",
        instrument="SPY",
    )  # fmt: skip
    states = ["LONG"] * 4 + ["FLAT"]
    st.process_many(build_inputs(bars, pd.Series(states, index=bars.index),
                                 flat_indicators(bars), times))  # fmt: skip
    d = last(st, "decision")
    assert d["exit_causes"] == "stop+strategy" and d["stop_activation"] is False
    intent = derive_intent(st.dir, CFG, before_due(st))
    assert intent is not None and intent.pending_kind == "exit" and intent.target_qty == 0.0


@pytest.mark.parametrize("policy", [OPG_WHOLE_SHARES])
def test_quantity_follows_the_configured_policy(tmp_path: Path, policy: str) -> None:
    st = account(tmp_path, "control_no_overlay", FLAT_THEN_LONG, ["FLAT"] * 5 + ["LONG"])
    cfg = BrokerConfig(order_policy=policy)
    intent = derive_intent(st.dir, cfg, before_due(st))
    assert intent is not None
    assert intent.target_qty == target_quantity(1.0, cfg.sizing_capital, 500.0, policy)


def test_no_intent_when_nothing_is_pending_or_due(tmp_path: Path) -> None:
    empty = paper_svc.paper_create_account(
        tmp_path / "a", strategy_id="sma_trend", strategy_version="1.0.0",
        scenario_name="control_no_overlay", instrument="SPY",
    )  # fmt: skip
    assert derive_intent(empty.dir, CFG, datetime(2024, 7, 1)) is None  # no decision yet
    flat = account(tmp_path / "b", "control_no_overlay", FLAT_THEN_LONG, ["FLAT"] * 6)
    assert last(flat, "decision")["pending_kind"] == "none"
    assert derive_intent(flat.dir, CFG, before_due(flat)) is None  # nothing to send
    entry = account(tmp_path / "c", "control_no_overlay", FLAT_THEN_LONG, ["FLAT"] * 5 + ["LONG"])
    due = pd.Timestamp(last(entry, "decision")["scheduled_execution_at"]).to_pydatetime()
    assert derive_intent(entry.dir, CFG, due) is None  # its session already opened: stale


@pytest.mark.parametrize(
    ("edit", "kind"),
    [
        (lambda s: s["progress"].__setitem__("last_session", "not-a-date"), "PAPER_STATE_CORRUPT"),
        (lambda s: s["identity"].__setitem__("config_fingerprint", "0" * 16),
         "PAPER_ACCOUNT_INCOMPATIBLE"),
    ],
)  # fmt: skip
def test_an_unreadable_account_raises(tmp_path: Path, edit, kind: str) -> None:  # type: ignore[no-untyped-def]
    st = account(tmp_path, "control_no_overlay", FLAT_THEN_LONG, ["FLAT"] * 5 + ["LONG"])
    state = json.loads(st.state_path.read_text(encoding="utf-8"))
    edit(state)
    st.state_path.write_text(json.dumps(state), encoding="utf-8")
    before = digest(st.dir)
    with pytest.raises(RuntimeError, match=kind):
        derive_intent(st.dir, CFG, before_due(st))
    assert digest(st.dir) == before
