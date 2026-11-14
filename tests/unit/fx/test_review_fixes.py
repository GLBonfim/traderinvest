"""Regression tests for the technical-review fixes E1-E3 and M1-M6 (2026-10-18).

All in memory or in temporary directories: fake adapters subclass the in-process simulator, no
network, no broker, no real clock waits (the engine clock is the simulated market clock).
"""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.safety import RealMoneyExecutionBlockedError
from app.engine.__main__ import STARTUP_REFUSED_EXIT
from app.engine.__main__ import main as engine_main
from app.engine.protocol import TOKEN_ENV
from app.engine.service import EngineService
from app.fx.config import FxConfig, load_config, save_config
from app.fx.environment import ENABLED, Environment, validate_environment
from app.fx.errors import (
    AccountMismatchError,
    EnvironmentMismatchError,
    JournalIntegrityError,
    StateCorruptError,
    StateUnreadableError,
)
from app.fx.lifecycle import TERMINAL, IllegalTransitionError, OrderState, can_transition
from app.fx.models import Direction
from app.fx.orchestrator import Intent, Orchestrator, ReconcilePolicy
from app.fx.orders import ExecutionError, OrderRequest, OrderStatus
from app.fx.ports import Capabilities
from app.fx.risk import RiskConfig, RiskManager, RiskState, TradeRequest
from app.fx.sim.broker import SimBroker, SimBrokerConfig
from app.fx.store import FxStore
from tests.unit.fx.helpers import NOW, orchestrator, small_config

# --- fake adapters (in-process, no network) ----------------------------------------------


class FakeLookupBroker(SimBroker):
    """The simulator with a NON-authoritative lookup: the first `hide` lookups return None
    (or raise, with `raise_lookups`), as a remote venue might. Counts submits and lookups."""

    def __init__(
        self,
        config: SimBrokerConfig,
        hide: int = 0,
        raise_lookups: int = 0,
        ambiguous_reject: bool = False,
    ) -> None:
        super().__init__(config)
        self.hide, self.raise_lookups = hide, raise_lookups
        self.ambiguous_reject = ambiguous_reject
        self.submits = 0
        self.lookups = 0

    @property
    def capabilities(self) -> Capabilities:
        return replace(super().capabilities, authoritative_order_lookup=False)

    def submit(self, request: OrderRequest) -> OrderStatus:
        self.submits += 1
        if self.ambiguous_reject:  # the venue rejects, but the reply is lost
            self.faults.reject_next = 1
            super().submit(request)
            raise ExecutionError("timeout after a rejection", ambiguous=True)
        return super().submit(request)

    def order(self, client_order_id: str) -> OrderStatus | None:
        self.lookups += 1
        if self.raise_lookups > 0:
            self.raise_lookups -= 1
            raise ExecutionError("venue unreachable", ambiguous=True)
        if self.hide > 0:
            self.hide -= 1
            return None
        return super().order(client_order_id)


class EnvBroker(SimBroker):
    """An adapter that declares another environment (to test the mismatch check)."""

    declared = Environment.DEMO

    @property
    def capabilities(self) -> Capabilities:
        return replace(super().capabilities, environment=self.declared)


class ScriptedBroker(SimBroker):
    """Accepts orders without filling them, then reports scripted venue statuses on lookup."""

    def __init__(self, config: SimBrokerConfig, script: list[tuple[OrderState, float]]) -> None:
        super().__init__(config)
        self.script = list(script)

    def submit(self, request: OrderRequest) -> OrderStatus:
        st = OrderStatus(request.client_order_id, "scripted-1", request.pair, request.side,
                         request.units, OrderState.ACCEPTED)  # fmt: skip
        self.orders[request.client_order_id] = st
        return st

    def order(self, client_order_id: str) -> OrderStatus | None:
        cur = self.orders.get(client_order_id)
        if cur is None:
            return None
        if self.script:
            state, filled = self.script.pop(0)
            cur = replace(cur, state=state, filled_units=filled, avg_price=1.08 if filled else None)
            self.orders[client_order_id] = cur
        return cur


def entry(o: Orchestrator, sym: str = "EURUSD", pid: str = "t-1") -> TradeRequest:
    q = o.market.quotes[sym]
    pip = 0.01 if sym.endswith("JPY") else 0.0001
    return TradeRequest(sym, Direction.LONG, round(q.mid - 30 * pip, 5),
                        round(q.mid + 60 * pip, 5), 0.9, pid)  # fmt: skip


def kinds(o: Orchestrator) -> list[str]:
    return [e["kind"] for e in o.store.recent(500)]


def db_rows(path: Path) -> tuple[list[Any], list[Any]]:
    db = sqlite3.connect(path)
    try:
        journal = db.execute("SELECT * FROM journal ORDER BY seq").fetchall()
        state = db.execute("SELECT key, value FROM state ORDER BY key").fetchall()
    finally:
        db.close()
    return journal, state


def sql(path: Path, statement: str, *params: Any) -> None:
    db = sqlite3.connect(path)
    db.execute(statement, params)
    db.commit()
    db.close()


def created(tmp_path: Path, cfg: FxConfig | None = None) -> Path:
    """A persisted account, closed cleanly; returns the database path."""
    o = orchestrator(tmp_path, cfg)
    o.process(entry(o), "trend")
    o.save()
    o.store.close()
    return tmp_path / "fx.db"


# --- E1: precedence of the daily locks -----------------------------------------------------


def risk_at(equity: float = 10_000.0, **cfg: Any) -> RiskManager:
    rm = RiskManager(RiskConfig(**cfg))
    rm.observe(equity, NOW)
    return rm


def test_e1_profit_target_then_loss_upgrades_to_daily_loss() -> None:
    rm = risk_at()
    assert rm.observe(11_600.0, NOW + timedelta(hours=1)) == ["lock:profit_target"]
    assert not rm.should_flatten()
    assert rm.observe(9_400.0, NOW + timedelta(hours=2)) == ["lock:daily_loss"]  # -6 %
    assert rm.state.lock == "daily_loss" and rm.should_flatten()
    # never downgraded, not even by a new high the same day
    assert rm.observe(12_000.0, NOW + timedelta(hours=3)) == []
    assert rm.state.lock == "daily_loss"


def test_e1_loss_limit_without_a_previous_target() -> None:
    rm = risk_at()
    assert rm.observe(9_499.0, NOW + timedelta(hours=1)) == ["lock:daily_loss"]
    assert rm.observe(9_000.0, NOW + timedelta(hours=2)) == []  # engaged once
    assert rm.should_flatten()


def test_e1_target_without_reaching_the_loss_limit() -> None:
    rm = risk_at()
    rm.observe(11_600.0, NOW + timedelta(hours=1))
    assert rm.observe(9_600.0, NOW + timedelta(hours=2)) == []  # -4 %: above the limit
    assert rm.state.lock == "profit_target" and not rm.should_flatten()


def test_e1_upgrade_survives_restarts_and_still_blocks_entries() -> None:
    rm = risk_at()
    rm.observe(11_600.0, NOW + timedelta(hours=1))
    again = RiskManager(rm.config, RiskState.from_dict(rm.state.to_dict()))
    assert again.observe(9_400.0, NOW + timedelta(hours=2)) == ["lock:daily_loss"]
    third = RiskManager(rm.config, RiskState.from_dict(again.state.to_dict()))
    third.observe(9_400.0, NOW + timedelta(hours=3))
    assert third.state.lock == "daily_loss" and third.should_flatten()


def test_e1_orchestrator_flattens_after_target_then_loss(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    assert o.process(entry(o), "trend").state is OrderState.FILLED  # type: ignore[union-attr]
    equity = o.broker.account().equity
    o.risk.state.day_start_equity = equity / 1.16  # pretend +16 % today
    o.tick()
    assert o.risk.state.lock == "profit_target" and o.broker.positions()  # kept (policy)
    o.risk.state.day_start_equity = o.broker.account().equity / 0.94  # now -6 % today
    o.tick()
    assert o.risk.state.lock == "daily_loss"
    assert o.broker.positions() == []  # the loss policy (flatten) applied
    assert kinds(o).count("risk_lock") == 2


# --- E2: one source of truth for the environment -------------------------------------------


def test_e2_validate_environment() -> None:
    assert frozenset({Environment.SIMULATION}) == ENABLED
    assert validate_environment(Environment.SIMULATION, Environment.SIMULATION) is (
        Environment.SIMULATION
    )
    with pytest.raises(EnvironmentMismatchError, match="não está ativo"):
        validate_environment(Environment.DEMO, Environment.DEMO)
    with pytest.raises(EnvironmentMismatchError, match="não está ativo"):
        validate_environment(Environment.DEMO, Environment.SIMULATION)
    for configured, adapter in ((Environment.REAL, Environment.SIMULATION),
                                (Environment.SIMULATION, Environment.REAL)):  # fmt: skip
        with pytest.raises(RealMoneyExecutionBlockedError):
            validate_environment(configured, adapter)


def test_e2_mismatched_adapter_cannot_start_and_processes_nothing(tmp_path: Path) -> None:
    with pytest.raises(EnvironmentMismatchError, match="adaptador implementa 'demo'"):
        Orchestrator(small_config(), FxStore(tmp_path / "fx.db"), NOW, broker_factory=EnvBroker)
    journal, state = db_rows(tmp_path / "fx.db")
    assert journal == [] and state == []  # refused before any processing or write


def test_e2_demo_configuration_is_refused_by_the_engine(tmp_path: Path) -> None:
    cfg = small_config().model_copy(update={"environment": Environment.DEMO})
    with pytest.raises(EnvironmentMismatchError):
        Orchestrator(cfg, FxStore(tmp_path / "fx.db"), NOW)
    assert db_rows(tmp_path / "fx.db") == ([], [])


def test_e2_real_stays_blocked(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="REAL"):
        FxConfig(environment=Environment.REAL)
    cfg = small_config().model_copy(update={"environment": Environment.REAL})  # bypassed
    with pytest.raises(RealMoneyExecutionBlockedError):
        Orchestrator(cfg, FxStore(tmp_path / "fx.db"), NOW)


def test_e2_snapshot_carries_the_validated_environment(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    snap = o.snapshot()
    assert snap["environment"] == "simulation" and snap["environment_validated"] is True
    assert snap["data_origin"] == "synthetic" and "simulado" in snap["adapter"]
    with pytest.raises(EnvironmentMismatchError):
        o.apply_config(o.config.model_copy(update={"environment": Environment.DEMO}))


# --- E3: orders with an unknown outcome -----------------------------------------------------


def flaky(tmp_path: Path, policy: ReconcilePolicy | None = None, **kw: Any) -> Orchestrator:
    return Orchestrator(
        small_config(),
        FxStore(tmp_path / "fx.db"),
        NOW,
        broker_factory=lambda c: FakeLookupBroker(c, **kw),
        reconcile=policy,
    )


def test_e3_inconclusive_then_fill_found_without_resend(tmp_path: Path) -> None:
    o = flaky(tmp_path, hide=1)
    br = o.broker
    assert isinstance(br, FakeLookupBroker)
    br.faults.timeout_next = 1
    it = o.process(entry(o), "trend")
    assert it is not None and it.state is OrderState.UNKNOWN and it.lookup_attempts == 1
    assert o.ambiguous_pairs() == {"EURUSD": it.client_order_id}
    blocked = o.process(entry(o, pid="t-2"), "trend")  # a new entry on the same pair
    assert blocked is not None and blocked.state is OrderState.REFUSED
    assert "resultado desconhecido" in blocked.reason
    assert o.reconcile_ambiguous()
    assert it.state is OrderState.FILLED and br.submits == 1  # found, never resent
    assert o.risk.state.entries_used == 1 and o.ambiguous_pairs() == {}


def test_e3_inconclusive_then_confirmed_rejection(tmp_path: Path) -> None:
    o = flaky(tmp_path, hide=1, ambiguous_reject=True)
    it = o.process(entry(o), "trend")
    assert it is not None and it.state is OrderState.UNKNOWN
    assert o.risk.entries_available == 4  # still reserved while unknown
    o.reconcile_ambiguous()
    assert it.state is OrderState.REJECTED and o.risk.entries_available == 5


def test_e3_lookup_failure_is_inconclusive(tmp_path: Path) -> None:
    o = flaky(tmp_path, raise_lookups=2)
    o.broker.faults.timeout_next = 1
    it = o.process(entry(o), "trend")
    assert it is not None and it.state is OrderState.UNKNOWN
    o.reconcile_ambiguous()
    assert it.state is OrderState.UNKNOWN and it.lookup_attempts == 2
    o.reconcile_ambiguous()
    assert it.state is OrderState.FILLED


def test_e3_attempt_limit_engages_the_fail_safe(tmp_path: Path) -> None:
    o = flaky(tmp_path, ReconcilePolicy(max_attempts=3), hide=999)
    br = o.broker
    assert isinstance(br, FakeLookupBroker)
    br.faults.timeout_next = 1
    it = o.process(entry(o), "trend")
    assert it is not None
    o.reconcile_ambiguous()
    assert not o.risk.state.kill_switch
    o.reconcile_ambiguous()  # third attempt: limit reached
    assert it.state is OrderState.UNKNOWN and it.reconcile_exhausted  # still unknown
    assert o.risk.state.kill_switch and "sem resolução" in str(o.risk.state.kill_reason)
    assert "order_unresolved" in kinds(o)
    lookups = br.lookups
    for _ in range(5):
        o.reconcile_ambiguous()
    assert br.lookups == lookups  # finite: no more automatic lookups
    assert br.submits == 1 and o.ambiguous_pairs() == {"EURUSD": it.client_order_id}
    assert o.risk.entries_available == 4  # the possible entry stays counted


def test_e3_age_limit_uses_the_engine_clock(tmp_path: Path) -> None:
    o = flaky(tmp_path, ReconcilePolicy(max_attempts=100, max_age=timedelta(minutes=2)), hide=999)
    o.broker.faults.timeout_next = 1
    it = o.process(entry(o), "trend")
    assert it is not None and not it.reconcile_exhausted
    o.market.now += timedelta(minutes=3)  # simulated time only
    o.reconcile_ambiguous()
    assert it.reconcile_exhausted and o.risk.state.kill_switch


def test_e3_authoritative_simulator_confirms_absence(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)

    def lost(request: OrderRequest) -> OrderStatus:  # never reached the in-process venue
        raise ExecutionError("connection dropped before sending", ambiguous=True)

    o.broker.submit = lost  # type: ignore[method-assign]
    it = o.process(entry(o), "trend")
    assert it is not None and it.state is OrderState.CANCELLED
    assert "ausência confirmada" in it.history[-1][2]
    assert o.risk.entries_available == 5 and not o.risk.state.kill_switch


def test_e3_restart_blocks_all_entries_until_reconciled(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    it = o.process(entry(o), "trend")
    assert it is not None and it.state is OrderState.FILLED
    it.state = OrderState.SENT  # crash after the venue filled, before the reply was stored
    o.save()
    o.store.close()
    r = Orchestrator(small_config(), FxStore(tmp_path / "fx.db"), NOW,
                     broker_factory=lambda c: FakeLookupBroker(c, hide=1))  # fmt: skip
    rec = r.intents[it.intent_id]
    assert rec.state is OrderState.UNKNOWN and r.startup_pending == {it.intent_id}
    other = r.process(entry(r, "GBPUSD", "g-1"), "trend")  # another pair: blocked too
    assert other is not None and other.state is OrderState.REFUSED
    assert "reconciliação de arranque" in other.reason
    r.reconcile_ambiguous()
    assert rec.state is OrderState.FILLED and r.startup_pending == set()
    later = r.process(entry(r, "GBPUSD", "g-2"), "trend")
    assert later is not None and later.state is OrderState.FILLED
    assert isinstance(r.broker, FakeLookupBroker) and r.broker.submits == 1  # only the new one


# --- M1: central validation of order transitions -------------------------------------------


def intent() -> Intent:
    return Intent("i", "p", "EURUSD", "buy", 1000, True, OrderState.PROPOSAL, NOW.isoformat(),
                  "c", None, None, None)  # fmt: skip


def test_m1_valid_path_and_invalid_jump() -> None:
    it = intent()
    path = (
        OrderState.AUTHORIZED,
        OrderState.SENT,
        OrderState.ACCEPTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    )
    for s in path:
        it.move(s, NOW)
    assert it.state is OrderState.FILLED
    other = intent()
    with pytest.raises(IllegalTransitionError):
        other.move(OrderState.FILLED, NOW)  # never from a proposal straight to a fill
    assert other.state is OrderState.PROPOSAL and other.history == []  # unchanged


def test_m1_terminal_states_have_no_way_out() -> None:
    for terminal in TERMINAL:
        for target in OrderState:
            assert not can_transition(terminal, target)
    it = intent()
    it.move(OrderState.REFUSED, NOW)
    with pytest.raises(IllegalTransitionError):
        it.move(OrderState.AUTHORIZED, NOW)


def test_m1_legitimate_states_are_preserved() -> None:
    assert can_transition(OrderState.SENT, OrderState.UNKNOWN)
    assert can_transition(OrderState.UNKNOWN, OrderState.FILLED)
    assert can_transition(OrderState.PARTIALLY_FILLED, OrderState.CANCELLED)
    assert can_transition(OrderState.PARTIALLY_FILLED, OrderState.EXPIRED)


def test_m1_repeated_processing_of_a_proposal_makes_no_new_transition(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    it = o.process(entry(o), "trend")
    assert it is not None
    history = list(it.history)
    assert o.process(entry(o), "trend") is it and it.history == history


# --- M2: corrupted persisted state ----------------------------------------------------------


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("UPDATE state SET value='{not json' WHERE key='risk'", "risk"),
        ("UPDATE state SET value='[1,2]' WHERE key='broker'", "broker"),
        ("UPDATE state SET value='{\"version\":1}' WHERE key='broker'", "broker"),
        ("UPDATE state SET value='{\"version\":2,\"intents\":{\"x\":{}}}' "
         "WHERE key='orchestrator'", "orchestrator"),
        ("DELETE FROM state WHERE key='market'", "incompleto"),
    ],
)  # fmt: skip
def test_m2_malformed_state_is_classified_and_left_untouched(
    tmp_path: Path, mutation: str, match: str
) -> None:
    db = created(tmp_path)
    sql(db, mutation)
    before = db_rows(db)
    with pytest.raises(StateCorruptError, match=match):
        Orchestrator(small_config(), FxStore(db), NOW)
    assert db_rows(db) == before  # nothing rewritten, no new account created


def test_m2_missing_state_with_history_is_not_a_first_start(tmp_path: Path) -> None:
    db = created(tmp_path)
    sql(db, "DELETE FROM state")
    before = db_rows(db)
    with pytest.raises(StateCorruptError, match="histórico"):
        Orchestrator(small_config(), FxStore(db), NOW)
    assert db_rows(db) == before


def test_m2_an_explicit_account_reset_may_start_fresh(tmp_path: Path) -> None:
    db = created(tmp_path)
    st = FxStore(db)
    st.append("account_reset", {"config": {}})
    st.clear_state()
    o = Orchestrator(small_config(), st, NOW)
    assert not o.recovered and o.broker.positions() == []


def test_m2_a_file_that_is_not_a_database(tmp_path: Path) -> None:
    db = tmp_path / "fx.db"
    db.write_bytes(b"this is not sqlite " * 100)
    with pytest.raises(StateUnreadableError):
        FxStore(db)
    assert db.read_bytes() == b"this is not sqlite " * 100


def test_m2_engine_refuses_with_a_classified_exit_and_no_restart_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    save_config(small_config(), tmp_path)
    db = created(tmp_path)
    sql(db, "UPDATE state SET value='{broken' WHERE key='risk'")
    monkeypatch.setenv(TOKEN_ENV, "t" * 40)
    assert engine_main(["--data-dir", str(tmp_path)]) == STARTUP_REFUSED_EXIT
    out = capsys.readouterr().out
    assert "ENGINE_ERROR startup:state_corrupt" in out and "broken" not in out
    assert not (tmp_path / "engine.json").exists()


def test_m2_invalid_configuration_is_classified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "config.json").write_text('{"environment": "real"}', encoding="utf-8")
    monkeypatch.setenv(TOKEN_ENV, "t" * 40)
    assert engine_main(["--data-dir", str(tmp_path)]) == STARTUP_REFUSED_EXIT
    assert "ENGINE_ERROR startup:config_invalid" in capsys.readouterr().out


def test_m2_valid_state_still_recovers(tmp_path: Path) -> None:
    created(tmp_path)
    assert Orchestrator(small_config(), FxStore(tmp_path / "fx.db"), NOW).recovered


# --- M3: journal hash chain verified at start-up -------------------------------------------


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE journal SET payload='{\"x\":1}' WHERE seq=1",  # altered event
        "UPDATE journal SET hash='" + "f" * 64 + "' WHERE seq=2",  # invalid hash
        "UPDATE journal SET prev_hash='" + "e" * 64 + "' WHERE seq=2",  # broken link
        "DELETE FROM journal WHERE seq=1",  # incomplete sequence
    ],
)
def test_m3_broken_chain_refuses_to_start(tmp_path: Path, mutation: str) -> None:
    db = created(tmp_path)
    sql(db, mutation)
    before = db_rows(db)
    with pytest.raises(JournalIntegrityError):
        Orchestrator(small_config(), FxStore(db), NOW)
    assert db_rows(db) == before  # not repaired, not rebuilt, not deleted


def test_m3_truncated_tail_is_detected_through_the_saved_head(tmp_path: Path) -> None:
    db = created(tmp_path)
    head = FxStore(db).head()[0]
    sql(db, "DELETE FROM journal WHERE seq >= ?", head)  # remaining chain is still valid
    assert FxStore(db).verify() == (True, None)
    with pytest.raises(JournalIntegrityError, match="truncado"):
        Orchestrator(small_config(), FxStore(db), NOW)


def test_m3_empty_and_valid_chains_start(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)  # empty chain at start
    assert o.store.verify() == (True, None)
    o.save()
    o.store.close()
    assert Orchestrator(small_config(), FxStore(tmp_path / "fx.db"), NOW).recovered


# --- M4: account currency coherence --------------------------------------------------------


def test_m4_matching_currency_recovers(tmp_path: Path) -> None:
    created(tmp_path)
    r = Orchestrator(small_config(), FxStore(tmp_path / "fx.db"), NOW)
    assert r.broker.book.currency == "EUR"


def test_m4_mismatch_refuses_without_touching_balances(tmp_path: Path) -> None:
    db = created(tmp_path)
    before = db_rows(db)
    usd = small_config().model_copy(update={"account_currency": "USD"})
    with pytest.raises(AccountMismatchError, match=r"EUR.*USD"):
        Orchestrator(usd, FxStore(db), NOW)
    assert db_rows(db) == before


def test_m4_missing_config_does_not_recurrency_a_persisted_account(tmp_path: Path) -> None:
    usd = small_config().model_copy(update={"account_currency": "USD"})
    created(tmp_path, usd)
    defaults = load_config(tmp_path)  # no config.json: provisional defaults (EUR)
    assert defaults.account_currency == "EUR"
    cfg = defaults.model_copy(update={"pairs": usd.pairs, "simulation": usd.simulation})
    with pytest.raises(AccountMismatchError):
        Orchestrator(cfg, FxStore(tmp_path / "fx.db"), NOW)


def test_m4_pairs_must_match_the_persisted_account(tmp_path: Path) -> None:
    db = created(tmp_path)
    before = db_rows(db)
    fewer = small_config().model_copy(update={"pairs": ["EURUSD", "GBPUSD"]})
    with pytest.raises(AccountMismatchError, match="pares"):
        Orchestrator(fewer, FxStore(db), NOW)
    assert db_rows(db) == before


def test_m4_currency_cannot_change_at_runtime(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    with pytest.raises(AccountMismatchError):
        o.apply_config(o.config.model_copy(update={"account_currency": "USD"}))


# --- M5: persisted safety pause ------------------------------------------------------------


def test_m5_safety_pause_survives_restart_and_needs_an_explicit_resume(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    o.pause("safety", "erro interno (RuntimeError)")
    o.store.close()
    r = Orchestrator(small_config(), FxStore(tmp_path / "fx.db"), NOW)
    assert r.paused and r.pause_reason == "safety"
    before = r.market.now
    r.tick()
    assert r.market.now == before  # no processing while paused
    with pytest.raises(ValueError, match="RETOMAR"):
        r.resume()  # a safety pause needs the typed confirmation
    assert r.paused
    r.resume("RETOMAR")
    assert not r.paused and "engine_resumed" in kinds(r)
    r.store.close()
    assert not Orchestrator(small_config(), FxStore(tmp_path / "fx.db"), NOW).paused


def test_m5_resume_never_clears_the_kill_switch_or_locks(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    o.engage_kill_switch("teste")
    o.pause("manual")
    o.store.close()
    r = Orchestrator(small_config(), FxStore(tmp_path / "fx.db"), NOW)
    r.resume()
    assert r.risk.state.kill_switch


def test_m5_engine_tick_error_persists_a_safety_pause(tmp_path: Path) -> None:
    save_config(small_config(), tmp_path)
    svc = EngineService(tmp_path, "t" * 40)
    svc._open()
    calls = {"n": 0}

    def boom() -> None:
        calls["n"] += 1
        raise RuntimeError("simulated internal failure")

    svc.orch.tick = boom  # type: ignore[method-assign]
    svc.safe_tick()
    svc.safe_tick()  # paused: the failing tick is not retried
    assert calls["n"] == 1 and svc.orch.pause_reason == "safety"
    svc._close()
    again = EngineService(tmp_path, "t" * 40)
    again._open()
    assert again.orch.paused and again.orch.pause_reason == "safety"
    with pytest.raises(ValueError):
        again._run_command("resume", {})  # no confirmation word
    assert again.orch.paused
    again._run_command("resume", {"confirmation": "RETOMAR"})
    assert not again.orch.paused
    again._close()


# --- M6: partial fills ---------------------------------------------------------------------


def scripted(tmp_path: Path, script: list[tuple[OrderState, float]]) -> Orchestrator:
    return Orchestrator(small_config(), FxStore(tmp_path / "fx.db"), NOW,
                        broker_factory=lambda c: ScriptedBroker(c, script))  # fmt: skip


def test_m6_several_partial_fills_then_complete(tmp_path: Path) -> None:
    o = scripted(tmp_path, [])
    req = entry(o)
    it = o.process(req, "trend")
    assert it is not None and it.state is OrderState.ACCEPTED
    units = it.units
    assert isinstance(o.broker, ScriptedBroker)
    o.broker.script = [(OrderState.PARTIALLY_FILLED, units * 0.25),
                       (OrderState.PARTIALLY_FILLED, units * 0.6),
                       (OrderState.FILLED, units)]  # fmt: skip
    assert [i["intent_id"] for i in o.snapshot()["open_intents"]] == [it.intent_id]
    o.reconcile_ambiguous()
    assert it.state is OrderState.PARTIALLY_FILLED and it.filled_units == units * 0.25
    assert it.remaining_units == units * 0.75
    o.reconcile_ambiguous()
    assert it.filled_units == units * 0.6  # cumulative from the venue, never added up
    o.reconcile_ambiguous()
    assert it.state is OrderState.FILLED and it.remaining_units == 0
    o.reconcile_ambiguous()  # a repeated report changes nothing
    assert [h[0] for h in it.history].count("partially_filled") == 2
    assert o.risk.state.entries_used == 1  # consumed once
    assert o.snapshot()["open_intents"] == []


def test_m6_remainder_cancelled_after_a_partial_fill(tmp_path: Path) -> None:
    o = scripted(tmp_path, [])
    it = o.process(entry(o), "trend")
    assert it is not None and isinstance(o.broker, ScriptedBroker)
    o.broker.script = [(OrderState.PARTIALLY_FILLED, 4000.0), (OrderState.CANCELLED, 4000.0)]
    o.reconcile_ambiguous()
    o.reconcile_ambiguous()
    assert it.state is OrderState.CANCELLED and it.filled_units == 4000
    assert it.label() == "Execução parcial — restante cancelado"
    assert o.risk.state.entries_used == 1 and o.snapshot()["open_intents"] == []


def test_m6_cancelled_without_any_fill_releases_the_entry(tmp_path: Path) -> None:
    o = scripted(tmp_path, [])
    it = o.process(entry(o), "trend")
    assert it is not None and o.risk.entries_available == 4
    assert isinstance(o.broker, ScriptedBroker)
    o.broker.script = [(OrderState.CANCELLED, 0.0)]
    o.reconcile_ambiguous()
    assert it.state is OrderState.CANCELLED and o.risk.entries_available == 5


def test_m6_simulator_partial_fill_is_final_and_counted_once(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    o.broker.faults.partial_next = 1
    it = o.process(entry(o), "trend")
    assert it is not None and it.state is OrderState.CANCELLED
    pos = o.broker.positions()
    assert len(pos) == 1 and pos[0].units == it.filled_units  # one position, filled size only
    assert o.risk.state.entries_used == 1
    assert it.intent_id not in {i["intent_id"] for i in o.snapshot()["open_intents"]}
    snap_intent = next(i for i in o.snapshot()["recent_intents"] if i["intent_id"] == it.intent_id)
    assert snap_intent["remaining_units"] == it.units - it.filled_units


def test_m6_rejection_still_releases(tmp_path: Path) -> None:
    o = orchestrator(tmp_path)
    o.broker.faults.reject_next = 1
    it = o.process(entry(o), "trend")
    assert it is not None and it.state is OrderState.REJECTED
    assert o.risk.entries_available == 5
