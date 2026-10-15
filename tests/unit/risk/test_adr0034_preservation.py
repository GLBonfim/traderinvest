"""ADR-0034 step 1: preservation and compatibility tests of the CURRENT behaviour.

Complements `test_adr0034_baseline.py` (left untouched) without duplicating it. Only synthetic
bars, in-memory engines (`RiskOverlay`, `run_risk_suite`, `PaperTradingEngine.replay`), the
frozen fixtures in `tests/fixtures/adr0034/` and `tmp_path` are used: no database, no paper
account, no operational ledger, no network.

Fixtures `paper_state_v1_*.json`: paper state documents of schema version 1 — the only schema
version that ever existed (`app/paper/state.py` has a single commit, `ba1a936`, with
`STATE_SCHEMA_VERSION = 1`). They were produced once by the current `app.paper.state.dumps`
from synthetic in-memory replays (strategy id `fixture_strategy`, so their account ids can
never coincide with an operational account) and are frozen: their SHA-256 is checked below and
must never be updated automatically.
"""

import dataclasses
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.backtest.config import BacktestConfig
from app.backtest.engine import session_times
from app.dashboard.services import paper as paper_svc
from app.paper.config import PaperConfig
from app.paper.engine import PaperTradingEngine
from app.paper.state import STATE_SCHEMA_VERSION, StateError, dumps, loads
from app.paper.trader import account_id_for
from app.risk.config import SCENARIOS, RiskConfig
from app.risk.diagnostics import run_risk_suite
from app.risk.limits import LOCKED, NORMAL
from app.validation.config import SliceSpec
from tests.unit.risk.test_adr0034_baseline import (
    ATR3,
    CONTROL,
    DD20,
    LOCKOUT_STATES,
    L,
    drawdown_path_bars,
    lockout_bars,
    stop_path_bars,
)
from tests.unit.risk.test_overlay import CAL, FREE, flat_indicators, make_bars, run
from tests.unit.strategies.helpers import session_walk, small_engine

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "adr0034"
USD_ATOL = 1e-6  # the tolerance of test_every_scenario_reproduces_phase11 (rtol 0)


def paper_replay(bars: pd.DataFrame, states: list[str], risk: RiskConfig, **kw: Any) -> Any:
    times = session_times(pd.DatetimeIndex(bars.index), CAL)
    engine = PaperTradingEngine(PaperConfig(risk=risk, backtest=FREE))
    series = pd.Series(states, index=bars.index)
    return engine.replay(bars, series, flat_indicators(bars), times, strategy_id="x", **kw)


# ── fingerprints and identity of the six pre-declared scenarios ──

# name -> (research fingerprint, paper fingerprint, paper account_id for sma_trend 1.0.0 SPY 1d)
CURRENT_IDENTITY = {
    "control_no_overlay": ("3d5570e854d0e703", "411f10ae26cb281f", "3f95a6964b8e"),
    "fixed_fraction_50": ("62de9e460e3037a2", "7bea53da40384644", "8fe5e52ca37c"),
    "volatility_target_10": ("2a5f8318894f63ae", "8960403889c54046", "5193036adcff"),
    "atr_risk_1pct": ("641bcb56a0480775", "4c2928cdd6c7134d", "e61501cfb52d"),
    "drawdown_lock_20": ("46967e83f8a3cbc8", "def3947a77643adc", "1ecfb5f0843c"),
    "atr_stop_3": ("f66a5a3d801afdc8", "3eb9676335813ff5", "7ca091f382ce"),
}
# ADR-0034 versioning: profiles with an enabled stop / drawdown lock / session lock get a
# per-mechanism semantic version; the others must keep their identity byte for byte.
PRESERVE = ("control_no_overlay", "fixed_fraction_50", "volatility_target_10", "atr_risk_1pct")
SEMANTIC_CHANGE_EXPECTED = ("drawdown_lock_20", "atr_stop_3")
# Identity after ADR-0034 (DRAWDOWN_SEMANTICS / STOP_SEMANTICS 2.0.0 in the payload), recorded
# deliberately in step H; CURRENT_IDENTITY above stays the pre-ADR record.
POST_ADR0034_IDENTITY = {
    "drawdown_lock_20": ("989fe7971a707205", "5dc60575258be11f", "4d46afd9443d"),
    "atr_stop_3": ("99566717f29b5310", "b48a0c434b4de2ef", "62d66e3174d3"),
}


def identity(risk: RiskConfig) -> tuple[str, str, str]:
    cfg = PaperConfig(risk=risk)
    return (
        risk.fingerprint(),
        cfg.fingerprint(),
        account_id_for(cfg, "sma_trend", "1.0.0", "SPY", "1d"),
    )


def has_event_mechanism(risk: RiskConfig) -> bool:
    return risk.stop.method != "none" or risk.drawdown.enabled or risk.session.enabled


def test_classification_covers_every_scenario_and_follows_the_configuration() -> None:
    names = [s.name for s in SCENARIOS]
    assert sorted(PRESERVE + SEMANTIC_CHANGE_EXPECTED) == sorted(names) == sorted(CURRENT_IDENTITY)
    for s in SCENARIOS:  # derived from the configuration, not asserted by hand
        assert has_event_mechanism(s) == (s.name in SEMANTIC_CHANGE_EXPECTED), s.name


@pytest.mark.parametrize("name", PRESERVE)
def test_identity_to_preserve(name: str) -> None:
    """Must hold before AND after the ADR-0034 implementation."""
    risk = next(s for s in SCENARIOS if s.name == name)
    assert identity(risk) == CURRENT_IDENTITY[name]


@pytest.mark.parametrize("name", SEMANTIC_CHANGE_EXPECTED)
def test_identity_semantic_change_expected(name: str) -> None:
    """ADR-0034 (D-A2 / D-A3 semantic versions) changed these identities — every component
    differs from the pre-ADR record and equals the value recorded in step H."""
    risk = next(s for s in SCENARIOS if s.name == name)
    now = identity(risk)
    assert all(a != b for a, b in zip(now, CURRENT_IDENTITY[name], strict=True))
    assert now == POST_ADR0034_IDENTITY[name]


def test_open_account_refuses_a_directory_whose_identity_does_not_match(tmp_path: Path) -> None:
    acct = tmp_path / "000000000000"  # not the account id the configuration would produce
    acct.mkdir()
    (acct / "state.json").write_text("{}", encoding="utf-8")
    ref = paper_svc.AccountRef(
        "000000000000", "sma_trend", "1.0.0", "control_no_overlay", "SPY", "1d", "", acct
    )
    with pytest.raises(paper_svc.AccountError, match="not one of the pre-declared scenarios"):
        paper_svc.open_account(ref, tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["000000000000"]  # nothing created


# ── lockout and cooldown ──


def test_cooldown_is_not_extended_by_a_further_fall() -> None:
    """D-A6 (current): a deeper fall during the lock neither extends nor restarts it."""
    path = [100, 100, 97, 93, 89, 85, 81, 78, 60, 55, 50] + [50] * 25 + [52, 55]
    bars = make_bars([(p, p + 0.5, p - 0.5, p) for p in path])
    r = run(bars, [L] * len(path), DD20)
    states = r.equity["risk_state"].tolist()
    first = states.index(LOCKED)
    assert states[first : first + 21] == [LOCKED] * 21 and states[first + 21] == NORMAL


def test_reentry_at_the_open_after_the_unlock_close() -> None:
    bars = drawdown_path_bars()
    r = run(bars, [L] * 43, DD20)
    states = r.equity["risk_state"].tolist()
    unlock = states.index(LOCKED) + 21  # first NORMAL close
    assert states[unlock] == NORMAL
    reentry = r.fills[r.fills["side"] == "buy"].iloc[-1]
    assert reentry["ts"] == bars.index[unlock + 1]  # decided at the unlock close


@pytest.mark.parametrize(
    ("name", "bars_fn", "states", "risk"),
    [("lockout_L_I_L_F", lockout_bars, LOCKOUT_STATES, ATR3),
     ("drawdown_path_dd20", drawdown_path_bars, [L] * 43, DD20),
     ("stop_path_atr3", stop_path_bars, [L] * 6, ATR3)],
)  # fmt: skip
def test_paper_reproduces_research_for_lockout_and_cooldown(
    name: str, bars_fn: Any, states: list[str], risk: RiskConfig
) -> None:
    """Same criterion as test_every_scenario_reproduces_phase11, on the step-0 paths."""
    bars = bars_fn()
    r = run(bars, states, risk)
    p = paper_replay(bars, states, risk)
    for col in ("equity", "cash", "costs"):
        np.testing.assert_allclose(
            p.equity[col], r.equity[col], rtol=0.0, atol=USD_ATOL, err_msg=col
        )
    np.testing.assert_allclose(p.equity["shares"], r.equity["shares"], rtol=1e-15, atol=0)
    np.testing.assert_array_equal(
        p.decisions["risk_approved_target"].to_numpy(), r.decisions["approved_exposure"].to_numpy()
    )
    assert len(p.fills) == len(r.fills)
    np.testing.assert_array_equal(p.fills["executed_at"], r.fills["ts"].to_numpy())
    assert p.portfolio["risk_state"].tolist() == r.equity["risk_state"].tolist()


# ── independence between research windows (suite slices) ──


SLICES = (
    SliceSpec("full"),
    SliceSpec("first", end=date(2015, 9, 30)),
    SliceSpec("second", start=date(2015, 10, 1)),
)


def test_each_slice_alone_equals_the_same_slice_inside_the_suite() -> None:
    bars = session_walk(300, seed=21).assign(adj_close=lambda b: b["close"])

    def suite(slices: tuple[SliceSpec, ...]) -> Any:
        return run_risk_suite(
            bars,
            scenarios=(CONTROL, DD20, ATR3),
            slices=slices,
            backtest=BacktestConfig(),
            strategy_engine=small_engine(),
            compare=False,
        )

    together = suite(SLICES)
    for sl in SLICES:
        alone = suite((sl,))
        assert set(alone.runs) == {k for k in together.runs if k[0] == sl.name}
        for key, r in alone.runs.items():
            pd.testing.assert_frame_equal(r.equity, together.runs[key].equity)
            pd.testing.assert_frame_equal(r.decisions, together.runs[key].decisions)
            pd.testing.assert_frame_equal(r.fills, together.runs[key].fills)


# ── initial LONG state in paper (convention A: same window start as the research engine) ──


def test_paper_replay_started_in_a_long_regime_enters_at_the_first_window_open() -> None:
    """`replay(start=…)` decides at the close of the session before `start` (convention A,
    approved after the Step 1 audit; Step 1 had frozen the former one-session delay): the
    entry is at the first open of the window, as a research window (`start_index`,
    `test_current_window_started_long`), with fresh capital and its own stop."""
    bars = make_bars([(100 + i, 101 + i, 99 + i, 100 + i) for i in range(10)])
    start = bars.index[3].tz_convert("America/New_York").date()
    p = paper_replay(bars, [L] * 10, ATR3, start=start)
    assert p.decisions.iloc[0]["bar_ts"] == bars.index[2]  # the decision before the window
    assert p.equity.index[0] == bars.index[3]  # results start at `start`
    assert p.fills.iloc[0]["executed_at"] == bars.index[3] and p.fills.iloc[0]["price"] == 103.0
    assert p.final_state.account.initial_capital == FREE.initial_capital


# ── paper state schema 1 (frozen fixtures) ──

FIXTURE_SHA256 = {
    "paper_state_v1_control.json": (
        "8970d80020b94b0564b03bb3b73272ad7312d5881239b3e73e4f44f844e487ea"
    ),
    "paper_state_v1_atr_stop_3.json": (
        "8bda6cddde65e074abf52057dd59534a5936ffcc9fee48470718ccfdb6617431"
    ),
}
FIXTURE_CONFIG = {
    "paper_state_v1_control.json": PaperConfig(risk=CONTROL),
    "paper_state_v1_atr_stop_3.json": PaperConfig(risk=ATR3),
}
V1_TOP_KEYS = {
    "schema_version", "paper_version", "identity", "account", "risk", "pending", "progress",
    "open_trade", "ledger",
}  # fmt: skip
V1_RISK_KEYS = {"drawdown", "session", "stop", "stop_lockout", "current_target"}


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(FIXTURE_SHA256))
def test_fixture_is_the_frozen_schema_1_document(name: str) -> None:
    raw = (FIXTURES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA256[name]
    d = json.loads(raw)
    assert d["schema_version"] == 1 and set(d) == V1_TOP_KEYS
    assert set(d["risk"]) == V1_RISK_KEYS  # no schema-2 field (e.g. stop_activated)
    assert d["identity"]["strategy_id"] == "fixture_strategy"
    # written before ADR-0034: the paper fingerprint of that time (pre-ADR record); unchanged
    # for the control, superseded for atr_stop_3 by STOP_SEMANTICS
    pre_adr = CURRENT_IDENTITY[FIXTURE_CONFIG[name].risk.name][1]
    assert d["identity"]["config_fingerprint"] == pre_adr


def test_schema_1_control_fixture_loads_and_is_rewritten_as_schema_2() -> None:
    """ADR-0034 D-A9: a schema-1 state without a stop profile loads with
    `stop_activated = false`; the next write is schema 2 (the same document plus the two
    schema-2 fields), and schema 2 round-trips byte for byte."""
    assert STATE_SCHEMA_VERSION == 2
    name = "paper_state_v1_control.json"
    v1 = json.loads(fixture_text(name))
    state, pointer = loads(fixture_text(name), FIXTURE_CONFIG[name])
    assert state.risk.stop_activated is False
    v2 = json.loads(dumps(state, pointer))
    expected = {**v1, "schema_version": 2, "risk": {**v1["risk"], "stop_activated": False}}
    assert v1["pending"] is not None  # the fixture holds a pending instruction (kind none)
    expected["pending"] = {**v1["pending"], "exit_causes": ""}
    assert v2 == expected
    again, ptr = loads(dumps(state, pointer), FIXTURE_CONFIG[name])
    assert dumps(again, ptr) == dumps(state, pointer)


def test_schema_1_control_fixture_has_no_stop() -> None:
    """Must keep loading after schema 2 (ADR-0034: v1 loads when the profile has no stop)."""
    d = json.loads(fixture_text("paper_state_v1_control.json"))
    assert d["risk"]["stop"] is None and CONTROL.stop.method == "none"
    state, _ = loads(
        fixture_text("paper_state_v1_control.json"), FIXTURE_CONFIG["paper_state_v1_control.json"]
    )
    assert state.account_id == "c42ae5a4e853" and state.account.shares > 0


def test_schema_1_stop_fixture_holds_an_active_stop() -> None:
    d = json.loads(fixture_text("paper_state_v1_atr_stop_3.json"))
    assert ATR3.stop.method == "atr" and d["risk"]["stop"] == 94.0 and d["account"]["shares"] > 0


def test_future_schema_1_with_an_active_stop_is_refused() -> None:
    """ADR-0034 D-A9 (implemented): the episode state is never inferred; the refusal names
    its reason (checked before the configuration fingerprint)."""
    with pytest.raises(StateError, match="stop episode state"):
        loads(
            fixture_text("paper_state_v1_atr_stop_3.json"),
            FIXTURE_CONFIG["paper_state_v1_atr_stop_3.json"],
        )


def test_fixture_configs_are_the_pre_declared_profiles() -> None:
    assert dataclasses.asdict(FIXTURE_CONFIG["paper_state_v1_control.json"].risk) == (
        dataclasses.asdict(SCENARIOS[0])
    )
    assert FIXTURE_CONFIG["paper_state_v1_atr_stop_3.json"].risk.name == "atr_stop_3"
