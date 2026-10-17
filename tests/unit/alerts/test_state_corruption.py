"""T1: a `state.json` with content the loader cannot parse (e.g. an invalid timestamp) is a
CORRUPT state (`StateError` -> `PAPER_STATE_CORRUPT`), never an unclassified `ValueError`; an
incompatible state stays `PAPER_ACCOUNT_INCOMPATIBLE`; and one bad account does not stop the
alert run for the others. Synthetic accounts in temporary directories only.
"""

import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from app.alerts.monitor import read_paper_account
from app.dashboard.services import paper as paper_svc
from app.paper.engine import baseline_inputs
from app.paper.state import StateCompatibilityError, StateError, loads
from tests.unit.alerts.test_engine_and_safety import bars, run  # noqa: F401

VT10 = "volatility_target_10"
INVALID_TIMESTAMPS = ["not-a-date", "2026-13-45T00:00:00+00:00", "9999-99-99"]


def account(root: Path, sid: str, frame: pd.DataFrame) -> Path:
    version, inputs = baseline_inputs(frame)[sid]
    st = paper_svc.paper_create_account(
        root, strategy_id=sid, strategy_version=version, scenario_name=VT10, instrument="SPY"
    )
    paper_svc.paper_process_sessions(st, inputs)
    return st.dir


def rewrite_state(d: Path, edit) -> None:  # type: ignore[no-untyped-def]
    state = json.loads((d / "state.json").read_text(encoding="utf-8"))
    edit(state)
    (d / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")


def digest(d: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(d.iterdir())}


@pytest.fixture
def healthy(tmp_path: Path, bars: pd.DataFrame) -> Path:  # noqa: F811
    return account(tmp_path / "paper", "rsi_momentum", bars)


# 1. the loader


@pytest.mark.parametrize("text", INVALID_TIMESTAMPS)
@pytest.mark.parametrize("field", ["last_session", "broker_last_session"])
def test_an_invalid_timestamp_is_a_state_error(healthy: Path, text: str, field: str) -> None:
    rewrite_state(healthy, lambda s: s["progress"].__setitem__(field, text))
    cfg = paper_svc.paper_config(VT10)
    with pytest.raises(StateError) as exc:
        loads((healthy / "state.json").read_text(encoding="utf-8"), cfg)
    assert not isinstance(exc.value, StateCompatibilityError)  # corrupt, not incompatible
    assert "timestamp" in str(exc.value)


def test_an_invalid_pending_timestamp_is_a_state_error(healthy: Path) -> None:
    state = json.loads((healthy / "state.json").read_text(encoding="utf-8"))
    assert state["pending"] is not None
    rewrite_state(healthy, lambda s: s["pending"].__setitem__("observed_at", "not-a-date"))
    with pytest.raises(StateError, match="timestamp"):
        loads((healthy / "state.json").read_text(encoding="utf-8"), paper_svc.paper_config(VT10))


# 2. the monitor


def test_monitor_classifies_an_invalid_timestamp_as_corrupt(healthy: Path) -> None:
    rewrite_state(healthy, lambda s: s["progress"].__setitem__("last_session", "not-a-date"))
    before = digest(healthy)
    acc = read_paper_account(healthy)
    assert acc.error_kind == "PAPER_STATE_CORRUPT" and "StateError" in acc.error
    assert digest(healthy) == before  # read-only


def test_monitor_still_reports_incompatibility(healthy: Path) -> None:
    rewrite_state(healthy, lambda s: s["identity"].__setitem__("config_fingerprint", "0" * 16))
    assert read_paper_account(healthy).error_kind == "PAPER_ACCOUNT_INCOMPATIBLE"


# 3. the alert engine keeps processing the healthy account


def test_engine_continues_past_corrupt_and_incompatible_accounts(
    tmp_path: Path,
    bars: pd.DataFrame,  # noqa: F811
) -> None:
    root = tmp_path / "paper"
    ok = account(root, "rsi_momentum", bars)
    corrupt = account(root, "sma_trend", bars)
    incompatible = account(root, "buy_and_hold", bars)
    rewrite_state(corrupt, lambda s: s["progress"].__setitem__("last_session", "not-a-date"))
    rewrite_state(incompatible, lambda s: s["identity"].__setitem__("config_fingerprint", "0" * 16))
    before = {d.name: digest(d) for d in (ok, corrupt, incompatible)}
    r = run(tmp_path, bars, since=date(2000, 1, 1))
    by_kind = {(a.event_type, a.account_id) for a in r.new_alerts}
    assert ("PAPER_STATE_CORRUPT", corrupt.name) in by_kind
    assert ("PAPER_ACCOUNT_INCOMPATIBLE", incompatible.name) in by_kind
    healthy_alerts = [a for a in r.new_alerts if a.account_id == ok.name]
    assert any(a.event_type == "PAPER_FILL_EXECUTED" for a in healthy_alerts)
    assert {d.name: digest(d) for d in (ok, corrupt, incompatible)} == before  # nothing repaired
