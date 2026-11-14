"""Final ADR-0034 audit fixes 2-4: no silent parallel paper account and a read-only `status`
(CLI), a specific classification for an incompatible account (alerts), and the minimum
`max_position_notional` (risk). Synthetic data and temporary directories only; the CLI runs
with its database, settings and logging replaced by in-memory fakes.
"""

import json
import math
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.alerts import monitor
from app.alerts.models import CRITICAL, EVENT_TYPES, make_alert
from app.paper import cli
from app.paper.config import PaperConfig
from app.paper.state import StateCompatibilityError, StateError, loads
from app.paper.store import PaperStore, conflicting_accounts
from app.risk.config import SCENARIOS, LimitSpec, RiskConfig
from app.risk.engine import RiskOverlay
from app.risk.manager import EPS, RiskManager
from tests.unit.alerts.test_engine_and_safety import _account, bars, run  # noqa: F401
from tests.unit.risk.test_overlay import FREE, make_bars
from tests.unit.strategies.helpers import session_walk

DD20 = next(s for s in SCENARIOS if s.name == "drawdown_lock_20")
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "adr0034"


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def fake_account(root: Path, name: str, **meta: Any) -> Path:
    """A directory with only a manifest (as `PaperStore` writes it): never opened here."""
    d = root / name
    d.mkdir(parents=True)
    body = {"account_id": name, "strategy_versions": {"sma_trend": "1.0.0"},
            "risk_scenario": "drawdown_lock_20", "instrument": "SPY", "timeframe": "1d",
            **meta}  # fmt: skip
    (d / "manifest.json").write_text(json.dumps(body), encoding="utf-8")
    return d


# ── fix 2: conflicting accounts (pure, read-only) ──


def conflicts(root: Path, **kw: Any) -> list[str]:
    args = {"account_id": "new000000000", "strategy_id": "sma_trend",
            "scenario": "drawdown_lock_20", "instrument": "SPY", "timeframe": "1d",
            **kw}  # fmt: skip
    return conflicting_accounts(root, **args)


def test_an_old_account_of_the_same_strategy_and_scenario_conflicts(tmp_path: Path) -> None:
    fake_account(tmp_path, "old000000000")
    assert conflicts(tmp_path) == ["old000000000"]


def test_the_current_identity_does_not_conflict(tmp_path: Path) -> None:
    fake_account(tmp_path, "new000000000")
    assert conflicts(tmp_path) == []


@pytest.mark.parametrize(
    "meta",
    [{"instrument": "QQQ"}, {"timeframe": "1h"}, {"risk_scenario": "atr_stop_3"},
     {"strategy_versions": {"rsi_momentum": "1.0.0"}}],
    ids=["instrument", "timeframe", "scenario", "strategy"],
)  # fmt: skip
def test_another_instrument_timeframe_scenario_or_strategy_does_not_conflict(
    tmp_path: Path, meta: dict[str, Any]
) -> None:
    fake_account(tmp_path, "old000000000", **meta)
    assert conflicts(tmp_path) == []


def test_no_manifest_or_no_root_means_no_conflict(tmp_path: Path) -> None:
    assert conflicts(tmp_path / "missing") == []
    (tmp_path / "empty_dir").mkdir()
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "manifest.json").write_text("{not json", encoding="utf-8")
    assert conflicts(tmp_path) == []


def test_only_direct_children_of_the_root_are_read(tmp_path: Path) -> None:
    fake_account(tmp_path / "nested", "old000000000")
    assert conflicts(tmp_path) == []


# ── fix 2: the CLI refuses a parallel account and `status` is read-only ──


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    walk = session_walk(160, seed=5)
    walk = walk.assign(adj_close=walk["close"])
    monkeypatch.setattr(
        cli, "get_settings", lambda: SimpleNamespace(log_level="INFO", log_format="json")
    )
    monkeypatch.setattr(cli, "configure_logging", lambda *a: None)
    monkeypatch.setattr(cli, "assert_safe_trading_mode", lambda s: None)
    monkeypatch.setattr(cli, "get_session_factory", lambda: lambda: nullcontext(None))
    monkeypatch.setattr(cli, "load_backtest_bars", lambda session, symbol: (None, walk))


def main(root: Path, command: str, capsys: Any) -> tuple[int, dict[str, Any]]:
    rc = cli.main([command, "--symbol", "SPY", "--strategy", "sma_trend",
                   "--scenario", "drawdown_lock_20", "--root", str(root)])  # fmt: skip
    out = capsys.readouterr().out
    start = 0 if out.startswith("{") else out.index("\n{") + 1  # log lines precede the JSON
    return rc, json.loads(out[start:])


def test_run_refuses_a_parallel_account_and_leaves_the_old_one_untouched(
    tmp_path: Path, cli_env: None, capsys: Any
) -> None:
    fake_account(tmp_path, "1ecfb5f0843c")  # the pre-ADR drawdown_lock_20 identity
    before = snapshot(tmp_path)
    rc, out = main(tmp_path, "run", capsys)
    acct = out["accounts"]["sma_trend"]
    assert rc == cli.EXIT_REFUSED and acct["status"] == "refused"
    assert acct["incompatible_accounts"] == ["1ecfb5f0843c"]
    assert "semantic version" in acct["error"] and acct["account_id"] in acct["error"]
    assert snapshot(tmp_path) == before  # nothing created, nothing changed


def test_run_creates_the_account_when_there_is_no_conflict(
    tmp_path: Path, cli_env: None, capsys: Any
) -> None:
    rc, out = main(tmp_path, "run", capsys)
    assert rc == 0 and out["accounts"]["sma_trend"]["processed"] > 0
    assert [p.name for p in tmp_path.iterdir()] == [out["accounts"]["sma_trend"]["account_id"]]


def test_status_without_an_account_creates_nothing(
    tmp_path: Path, cli_env: None, capsys: Any
) -> None:
    rc, out = main(tmp_path, "status", capsys)
    assert rc == 0 and out["accounts"]["sma_trend"]["status"] == "no_account"
    assert out["accounts"]["sma_trend"]["incompatible_accounts"] == []
    assert list(tmp_path.iterdir()) == []
    fake_account(tmp_path, "1ecfb5f0843c")
    before = snapshot(tmp_path)
    rc, out = main(tmp_path, "status", capsys)
    assert out["accounts"]["sma_trend"]["incompatible_accounts"] == ["1ecfb5f0843c"]
    assert snapshot(tmp_path) == before


def test_status_of_an_existing_account_changes_nothing(
    tmp_path: Path, cli_env: None, capsys: Any
) -> None:
    main(tmp_path, "run", capsys)
    before = snapshot(tmp_path)
    rc, out = main(tmp_path, "status", capsys)
    assert rc == 0 and out["accounts"]["sma_trend"]["sessions_processed"] > 0
    assert snapshot(tmp_path) == before


# ── fix 3: classification of an unreadable account ──


def state_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_incompatibility_is_a_specific_state_error() -> None:
    v1_stop = state_text("paper_state_v1_atr_stop_3.json")
    with pytest.raises(StateCompatibilityError, match="stop episode"):
        loads(v1_stop, PaperConfig(risk=next(s for s in SCENARIOS if s.name == "atr_stop_3")))
    control = state_text("paper_state_v1_control.json")
    with pytest.raises(StateCompatibilityError, match="semantic version"):
        loads(control, PaperConfig(risk=DD20))  # another configuration
    d = json.loads(control)
    d["schema_version"] = 3
    with pytest.raises(StateCompatibilityError, match="unsupported"):
        loads(json.dumps(d), PaperConfig(risk=SCENARIOS[0]))
    for bad in ("2", None):  # not a version at all: corrupt, not incompatible
        d["schema_version"] = bad
        with pytest.raises(StateError) as exc:
            loads(json.dumps(d), PaperConfig(risk=SCENARIOS[0]))
        assert not isinstance(exc.value, StateCompatibilityError)
    d = json.loads(control)
    d["account"]["cash"] = -1.0
    with pytest.raises(StateError) as exc2:
        loads(json.dumps(d), PaperConfig(risk=SCENARIOS[0]))
    assert not isinstance(exc2.value, StateCompatibilityError)


def account_dir(root: Path) -> Path:
    s = PaperStore(PaperConfig(risk=DD20), strategy_id="sma_trend", strategy_version="1.0.0",
                   root=root)  # fmt: skip
    bars_ = make_bars([(100.0 + i, 101.0 + i, 99.0 + i, 100.0 + i) for i in range(4)])
    from app.backtest.engine import session_times
    from app.paper.engine import build_inputs
    from tests.unit.risk.test_overlay import CAL, flat_indicators

    times = session_times(pd.DatetimeIndex(bars_.index), CAL)
    s.process_many(build_inputs(bars_, pd.Series(["LONG"] * 4, index=bars_.index),
                                flat_indicators(bars_), times))  # fmt: skip
    return s.dir


@pytest.mark.parametrize(
    ("damage", "kind"),
    [
        ("fingerprint", "PAPER_ACCOUNT_INCOMPATIBLE"),
        ("schema", "PAPER_ACCOUNT_INCOMPATIBLE"),
        ("field", "PAPER_STATE_CORRUPT"),
        ("unreadable", "PAPER_STATE_CORRUPT"),
        ("ledger", "PAPER_LEDGER_VERIFICATION_FAILED"),
        ("scenario", "PAPER_ACCOUNT_ERROR"),
    ],
)
def test_each_classification_and_its_message(tmp_path: Path, damage: str, kind: str) -> None:
    d = account_dir(tmp_path)
    state = json.loads((d / "state.json").read_text(encoding="utf-8"))
    if damage == "fingerprint":
        state["identity"]["config_fingerprint"] = "def3947a77643adc"  # pre-ADR dd20 paper fp
    elif damage == "schema":
        state["schema_version"] = 9
    elif damage == "field":
        state["account"].pop("cash")
    if damage in ("fingerprint", "schema", "field"):
        (d / "state.json").write_text(json.dumps(state), encoding="utf-8")
    elif damage == "unreadable":
        (d / "state.json").unlink()
    elif damage == "ledger":
        text = (d / "ledger.jsonl").read_text(encoding="utf-8")
        (d / "ledger.jsonl").write_text(text.replace("sma_trend", "tampered", 1), encoding="utf-8")
    else:
        m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        m["risk_scenario"] = "not_a_scenario"
        (d / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
    before = snapshot(tmp_path)
    acc = monitor.read_paper_account(d)
    assert acc.error_kind == kind, acc.error
    if kind == "PAPER_ACCOUNT_INCOMPATIBLE":
        assert "StateCompatibilityError" in acc.error
    assert snapshot(tmp_path) == before  # read-only


def test_the_incompatible_kind_is_a_registered_critical_alert() -> None:
    assert EVENT_TYPES["PAPER_ACCOUNT_INCOMPATIBLE"][0] == CRITICAL
    a = make_alert("PAPER_ACCOUNT_INCOMPATIBLE", key_parts=("x",), occurred_at="2026-10-14",
                   title="t", message="m", payload={})  # fmt: skip
    assert a.severity == CRITICAL


def test_the_alert_engine_reports_an_incompatible_account_once(
    tmp_path: Path,
    bars: pd.DataFrame,  # noqa: F811
) -> None:
    acct = _account(tmp_path / "paper", bars)
    state = json.loads((acct / "state.json").read_text(encoding="utf-8"))
    state["identity"]["config_fingerprint"] = "0" * 16  # another configuration
    (acct / "state.json").write_text(json.dumps(state), encoding="utf-8")
    before = (acct / "state.json").read_bytes()
    r = run(tmp_path, bars)
    got = [a for a in r.new_alerts if a.event_type == "PAPER_ACCOUNT_INCOMPATIBLE"]
    assert len(got) == 1 and "semantic version" in got[0].message
    assert "PAPER_STATE_CORRUPT" not in [a.event_type for a in r.new_alerts]
    assert (acct / "state.json").read_bytes() == before


# ── fix 4: minimum max_position_notional ──


def manager(notional: float, capital: float) -> RiskManager:
    return RiskManager(RiskConfig("n", limits=LimitSpec(max_position_notional=notional)), capital)


@pytest.mark.parametrize("capital", [1.0, 10_000.0, 100_000.0])
def test_notional_at_or_below_the_minimum_is_refused(capital: float) -> None:
    at = EPS * capital
    while at / capital > EPS:  # the representable boundary for this capital
        at = float(np.nextafter(at, 0.0))
    with pytest.raises(ValueError, match="too small"):
        manager(at, capital)  # exactly at the boundary
    with pytest.raises(ValueError, match="too small"):
        manager(at / 2, capital)  # below
    above = float(np.nextafter(at, math.inf))
    while not above / capital > EPS:
        above = float(np.nextafter(above, math.inf))
    manager(above, capital)  # the first value above the boundary is valid


def test_engines_refuse_the_configuration_before_simulating() -> None:
    risk = RiskConfig("n", limits=LimitSpec(max_position_notional=1e-9))
    bars_ = make_bars([(100.0, 101.0, 99.0, 100.0)] * 3)
    from app.backtest.engine import session_times
    from tests.unit.risk.test_overlay import CAL, flat_indicators

    times = session_times(pd.DatetimeIndex(bars_.index), CAL)
    with pytest.raises(ValueError, match="too small"):
        RiskOverlay(risk, FREE).run(bars_, pd.Series(["LONG"] * 3, index=bars_.index),
                                    flat_indicators(bars_), times, strategy_id="x")  # fmt: skip


def test_equity_growing_past_the_minimum_fails_explicitly() -> None:
    rm = manager(1e-6, 100.0)  # valid at the start (1e-8 of the equity)
    rm.current_target = 1.0
    with pytest.raises(ValueError, match="too small for the current equity"):
        rm.decide(requested=1.0, close=100.0, realized_vol=0.2, atr=2.0, equity=1e7,
                  triggered=False, touch=False, strategy_state="LONG")  # fmt: skip


@pytest.mark.parametrize("risk", SCENARIOS, ids=lambda r: r.name)
def test_pre_declared_profiles_are_unaffected(risk: RiskConfig) -> None:
    assert risk.limits.max_position_notional is None
    RiskManager(risk, FREE.initial_capital)
    small = RiskConfig("s", limits=LimitSpec(max_position_notional=100.0))
    d = RiskManager(small, 100_000.0).decide(
        requested=1.0, close=100.0, realized_vol=0.2, atr=2.0, equity=100_000.0,
        triggered=False, touch=False, strategy_state="LONG",
    )  # fmt: skip
    assert d.approved_exposure == 100.0 / 100_000.0 and d.reasons == ("exposure_limit",)
