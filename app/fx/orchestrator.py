"""Central orchestrator: runs the agents on each closed bar, sends the coordinator's requests to
the risk manager, executes what is authorized and records every lifecycle step.

Flow per decision bar (ADR-0036):
    agents -> AgentProposal (SIGNAL/PROPOSAL) -> coordinator -> TradeRequest
    -> RiskManager.evaluate -> AUTHORIZED | REFUSED
    -> write-ahead journal + state (SENT) -> broker.submit
    -> ACCEPTED / REJECTED / PARTIALLY_FILLED / FILLED, or UNKNOWN (ambiguous)
    -> UNKNOWN is resolved by looking the order up by client order id, never by resending.

Every tick also feeds quotes to the broker (server-side stops), observes equity for the daily
limits and flattens positions when a lock requires it. State is saved atomically (market,
broker, risk, orchestrator) after every order event and periodically.

Start-up (before any processing) refuses, with a classified `FxStartupError`:
- an environment that the configuration, the adapter and the enabled set do not agree on;
- a journal whose hash chain is broken, or shorter than the saved state expects;
- state documents that are malformed or incomplete, or missing while the journal has history;
- a configured account currency different from the persisted account's.
Nothing is repaired or recreated automatically.

Orders with an unknown outcome (ADR-0036): an empty lookup proves absence only when the
adapter declares `authoritative_order_lookup`; otherwise the order stays UNKNOWN, entries on
its pair are blocked, the lookup is retried on each tick up to `ReconcilePolicy` (attempts and
engine-clock age) and, if still unresolved, the kill switch is engaged and an
`order_unresolved` alert is journaled. Orders still unknown after a restart also block every
new entry until they are reconciled.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.fx.agents import (
    MeanReversionAgent,
    MomentumAgent,
    PortfolioCoordinator,
    RegimeAgent,
    TrendAgent,
)
from app.fx.agents.base import Agent, MarketView
from app.fx.agents.coordinator import PairDecision
from app.fx.calendar import FxCalendar
from app.fx.config import FxConfig
from app.fx.environment import LABELS as ENV_LABELS
from app.fx.environment import require_executable, validate_environment
from app.fx.errors import (
    AccountMismatchError,
    EnvironmentMismatchError,
    JournalIntegrityError,
    StateCorruptError,
)
from app.fx.lifecycle import LABELS as STATE_LABELS
from app.fx.lifecycle import TERMINAL, OrderState, transition
from app.fx.models import Direction, Side
from app.fx.orders import ExecutionError, Fill, OrderRequest, OrderStatus
from app.fx.proposals import AgentProposal, AgentRole, ProposalAction
from app.fx.risk import RiskDecision, RiskManager, RiskState, TradeRequest
from app.fx.sim.broker import SimBroker, SimBrokerConfig
from app.fx.sim.market import SyntheticMarket
from app.fx.store import FxStore

ENGINE_VERSION = "0.1.0"
SAVE_EVERY_TICKS = 30
STATE_VERSIONS = (1, 2)  # orchestrator state document: 2 adds journal_head and pause
EPS_UNITS = 1e-9
VENUE_LEVERAGE = 30.0  # simulated venue margin (ESMA retail, majors); risk caps exposure below
Listener = Callable[[dict[str, Any]], None]
UI_EVENT_KINDS = (
    "account_created",
    "engine_recovered",
    "trading_day_started",
    "risk_lock",
    "risk_decision",
    "order_sent",
    "order_result",
    "order_unknown",
    "position_closed",
    "flatten_all",
    "kill_switch",
    "config_changed",
    "engine_paused",
    "engine_resumed",
    "order_unresolved",
)
STATE_KEYS = ("market", "broker", "risk", "orchestrator")


@dataclass(frozen=True)
class ReconcilePolicy:
    """Bounded reconciliation of an order with an unknown outcome. Age is measured on the
    engine clock (simulated time in SIMULATION), so no real waiting is involved."""

    max_attempts: int = 5
    max_age: timedelta = timedelta(minutes=5)


class LookupOutcome(StrEnum):
    FOUND = "found"  # the venue returned the order: its status is authoritative
    ABSENT_CONFIRMED = "absent_confirmed"  # empty lookup AND the adapter guarantees absence
    INCONCLUSIVE = "inconclusive"  # empty lookup without that guarantee, or lookup failure


@dataclass
class Intent:
    intent_id: str
    proposal_id: str
    pair: str
    side: str
    units: float
    is_entry: bool
    state: OrderState
    created_at: str
    client_order_id: str
    lead_agent: str | None
    stop_loss: float | None
    take_profit: float | None
    history: list[list[str]] = field(default_factory=list)  # [state, ts, note]
    filled_units: float = 0.0
    avg_price: float | None = None
    reason: str = ""
    unknown_since: str | None = None  # engine-clock time the outcome became unknown
    lookup_attempts: int = 0
    reconcile_exhausted: bool = False

    def move(self, state: OrderState, ts: datetime, note: str = "") -> None:
        """The only way an order changes state: validated by `lifecycle.transition`
        (raises `IllegalTransitionError`)."""
        self.state = transition(self.state, state)
        self.history.append([state.value, ts.isoformat(), note])

    @property
    def remaining_units(self) -> float:
        return max(0.0, self.units - self.filled_units)

    def label(self) -> str:
        if self.state is OrderState.CANCELLED and self.filled_units > EPS_UNITS:
            return "Execução parcial — restante cancelado"
        return STATE_LABELS[self.state]

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.__dict__,
            "state": self.state.value,
            "state_label": self.label(),
            "remaining_units": self.remaining_units,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Intent":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        data["state"] = OrderState(data["state"])
        return cls(**data)


def compact_event(e: dict[str, Any]) -> dict[str, Any]:
    """A journal event reduced to what the UI log shows (no bulky evidence)."""
    p = e["payload"]
    kind = e["kind"]
    if kind == "risk_decision":
        d = p["decision"]
        p = {
            "intent_id": p["intent_id"],
            "pair": p["request"]["pair"],
            "direction": p["request"]["direction"],
            "approved": d["approved"],
            "summary": d["summary"],
        }
    elif kind == "order_result":
        st = p.get("status", {})
        p = {
            "intent_id": p["intent_id"],
            "state": p["state"],
            "pair": st.get("pair"),
            "side": st.get("side"),
            "filled_units": st.get("filled_units"),
            "avg_price": st.get("avg_price"),
            "reason": st.get("reason", p.get("reason")),
        }
    elif kind == "order_sent":
        o = p["order"]
        p = {
            "intent_id": p["intent_id"],
            "pair": o["pair"],
            "side": o["side"],
            "units": o["units"],
            "reduce_only": o["reduce_only"],
        }
    elif kind == "cycle":
        p = {
            "cycle": p["cycle"],
            "selected": p["selected"],
            "signals": sum(1 for d in p["decisions"] if d["action"] != "hold"),
        }
    elif kind == "config_changed":
        p = {}
    return {**e, "payload": p}


def intent_id_for(proposal_id: str) -> str:
    return "int-" + hashlib.sha256(proposal_id.encode()).hexdigest()[:16]


class Orchestrator:
    def __init__(
        self,
        config: FxConfig,
        store: FxStore,
        now: datetime | None = None,
        *,
        broker_factory: Callable[[SimBrokerConfig], SimBroker] = SimBroker,
        reconcile: ReconcilePolicy | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.environment = require_executable(config.environment)  # REAL refused first
        self.broker_factory = broker_factory
        self.reconcile_policy = reconcile or ReconcilePolicy()
        self.calendar = FxCalendar()
        self.listeners: list[Listener] = []
        self.paused = False
        self.pause_reason: str | None = None  # "manual" | "safety"
        self.pause_detail = ""
        self.paused_at: str | None = None
        self.startup_pending: set[str] = set()  # intents still UNKNOWN after the restart
        self.agents: list[Agent] = [TrendAgent(), MomentumAgent(), MeanReversionAgent()]
        self.regime_agent = RegimeAgent()
        self.coordinator = PortfolioCoordinator(
            config.risk.min_score, config.max_new_entries_per_cycle
        )
        self.intents: dict[str, Intent] = {}
        self.owners: dict[str, str] = {}  # pair -> agent role that opened the position
        self.last_proposals: dict[str, dict[str, dict[str, Any]]] = {}  # pair -> role -> p
        self.last_decisions: dict[str, dict[str, Any]] = {}
        self.agent_status: dict[str, dict[str, Any]] = {}
        self.cycles = 0
        self.ticks = 0
        self.equity_curve: list[list[Any]] = []
        self.recovered = False
        self._restore_or_create(now or datetime.now(UTC))

    # --- construction / recovery ----------------------------------------------------------

    def _broker_config(self) -> SimBrokerConfig:
        c = self.config
        return SimBrokerConfig(
            currency=c.account_currency,
            initial_balance=c.initial_capital,
            leverage=VENUE_LEVERAGE,
            commission_per_million=c.simulation.commission_per_million,
            slippage_pips=c.simulation.slippage_pips,
        )

    def _restore_or_create(self, now: datetime) -> None:
        st = self.store
        self.broker = self.broker_factory(self._broker_config())
        caps = self.broker.capabilities
        # E2: one source of truth, checked before anything is read or processed
        self.environment = validate_environment(self.config.environment, caps.environment)
        # M3: the journal must be intact before the engine accepts work
        ok, bad = st.verify()
        if not ok:
            raise JournalIntegrityError(
                f"a cadeia de hashes do registo de eventos falha no evento {bad}"
            )
        docs = self._load_state_documents()
        if docs:
            self._restore(docs)
        else:
            if st.count() and st.last_kind() != "account_reset":
                # M2: a journal with history but no state is not a first start
                raise StateCorruptError(
                    "o estado guardado não existe mas o registo de eventos tem histórico; "
                    "uma conta nova não é criada automaticamente"
                )
            self._create(now)
        if self.market.origin is not caps.data_origin:
            raise EnvironmentMismatchError(
                f"o adaptador declara dados '{caps.data_origin.value}' mas o mercado fornece "
                f"'{self.market.origin.value}'"
            )
        self.risk.observe(self.broker.account().equity, self.market.now)
        self.save()

    def _load_state_documents(self) -> dict[str, dict[str, Any]]:
        """All four state documents, or {} on a first start. A partial set is corrupt."""
        docs = {k: self.store.get_state(k) for k in STATE_KEYS}
        present = {k: v for k, v in docs.items() if v is not None}
        if present and len(present) != len(STATE_KEYS):
            missing = ", ".join(k for k in STATE_KEYS if k not in present)
            raise StateCorruptError(f"estado guardado incompleto: falta(m) {missing}")
        return present

    def _parse(self, key: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            raise StateCorruptError(
                f"documento de estado '{key}' inválido ({type(exc).__name__})"
            ) from exc

    def _restore(self, docs: dict[str, dict[str, Any]]) -> None:
        orch_d = docs["orchestrator"]
        self.market = self._parse(
            "market", lambda: SyntheticMarket.from_dict(docs["market"], self.calendar)
        )
        self._parse("broker", lambda: self.broker.load(docs["broker"]))
        # M4: the configuration must describe the persisted account, never the reverse
        if self.broker.book.currency != self.config.account_currency:
            raise AccountMismatchError(
                f"a conta guardada está em {self.broker.book.currency} mas a configuração pede "
                f"{self.config.account_currency}; repor a moeda na configuração ou reiniciar a "
                "conta de forma explícita"
            )
        risk_state = self._parse("risk", lambda: RiskState.from_dict(docs["risk"]))

        def orchestrator_doc() -> None:
            if orch_d.get("version") not in STATE_VERSIONS:
                raise ValueError("unsupported orchestrator state version")
            self.intents = {k: Intent.from_dict(v) for k, v in orch_d["intents"].items()}
            self.owners = {str(k): str(v) for k, v in dict(orch_d.get("owners", {})).items()}
            self.cycles = int(orch_d.get("cycles", 0))
            self.equity_curve = list(orch_d.get("equity_curve", []))
            pause = dict(orch_d.get("pause") or {})
            self.paused = bool(pause.get("paused", False))
            self.pause_reason = pause.get("reason")
            self.pause_detail = str(pause.get("detail") or "")
            self.paused_at = pause.get("at")

        self._parse("orchestrator", orchestrator_doc)
        head = orch_d.get("journal_head")
        if head is not None:
            seq, digest = self._parse("orchestrator", lambda: (int(head["seq"]), str(head["hash"])))
            if seq and self.store.hash_at(seq) != digest:
                raise JournalIntegrityError(
                    f"o registo de eventos não contém o evento {seq} referido pelo estado "
                    "guardado (truncado ou substituído)"
                )
        for f in self.broker.on_quotes(self.market.quotes):  # normally none: saved together
            self._on_venue_fill(f)
        self.broker.collect_fills()
        self.risk = RiskManager(
            self.config.risk.to_risk_config(), risk_state, self.calendar, self.environment
        )
        self.recovered = True
        self.journal(
            "engine_recovered",
            {
                "intents": len(self.intents),
                "positions": len(self.broker.positions()),
                "paused": self.paused,
                "pause_reason": self.pause_reason,
            },
        )
        self._startup_reconcile()

    def _create(self, now: datetime) -> None:
        sim = self.config.simulation
        start = now - timedelta(minutes=sim.warm_up_minutes)
        self.market = SyntheticMarket(self.config.pairs, start, sim.seed, self.calendar)
        self.market.warm_up(sim.warm_up_minutes)
        self.broker.on_quotes(self.market.quotes)
        self.risk = RiskManager(
            self.config.risk.to_risk_config(), None, self.calendar, self.environment
        )
        self.risk.observe(self.broker.account().equity, self.market.now)
        self.save()  # state first: a crash before the journal entry is not "state missing"
        self.journal(
            "account_created",
            {
                "environment": self.environment.value,
                "currency": self.config.account_currency,
                "capital": self.config.initial_capital,
                "pairs": self.config.pairs,
                "data_origin": self.market.origin.value,
            },
        )

    # --- reconciliation of orders with an unknown outcome (E3) ----------------------------

    def _lookup(self, client_order_id: str) -> tuple[LookupOutcome, OrderStatus | None]:
        try:
            status = self.broker.order(client_order_id)
        except ExecutionError:
            return LookupOutcome.INCONCLUSIVE, None
        if status is not None:
            return LookupOutcome.FOUND, status
        if self.broker.capabilities.authoritative_order_lookup:
            return LookupOutcome.ABSENT_CONFIRMED, None
        return LookupOutcome.INCONCLUSIVE, None

    def _mark_unknown(self, it: Intent, note: str) -> None:
        it.move(OrderState.UNKNOWN, self.market.now, note)
        it.unknown_since = self.market.now.isoformat()
        it.lookup_attempts = 0
        it.reconcile_exhausted = False

    def _reconcile_intent(self, it: Intent, recovered: bool = False) -> bool:
        """One bounded lookup for an UNKNOWN order. True when its outcome is now known."""
        now = self.market.now
        outcome, status = self._lookup(it.client_order_id)
        if outcome is LookupOutcome.FOUND and status is not None:
            self._apply_status(it, status, recovered=recovered)
            return True
        if outcome is LookupOutcome.ABSENT_CONFIRMED:
            it.move(OrderState.CANCELLED, now, "ausência confirmada pelo corretor: não aceite")
            if it.is_entry:
                self.risk.release(it.intent_id)
            self.journal(
                "order_result",
                {"intent_id": it.intent_id, "state": it.state.value, "reason": "absent"},
            )
            return True
        it.lookup_attempts += 1
        since = datetime.fromisoformat(it.unknown_since) if it.unknown_since else now
        policy = self.reconcile_policy
        if not it.reconcile_exhausted and (
            it.lookup_attempts >= policy.max_attempts or now - since >= policy.max_age
        ):
            self._fail_safe(it)
        return False

    def _fail_safe(self, it: Intent) -> None:
        """Still unknown after the bounded attempts: stop retrying, keep the order UNKNOWN
        (it may exist), keep its pair blocked, engage the kill switch and raise an alert."""
        it.reconcile_exhausted = True
        it.reason = (
            f"resultado desconhecido após {it.lookup_attempts} consulta(s): kill switch ativado"
        )
        self.risk.engage_kill_switch(f"ordem {it.client_order_id} sem resolução", self.market.now)
        self.journal(
            "order_unresolved",
            {
                "intent_id": it.intent_id,
                "client_order_id": it.client_order_id,
                "pair": it.pair,
                "attempts": it.lookup_attempts,
                "unknown_since": it.unknown_since,
            },
        )
        self.journal("kill_switch", {"engaged": True, "reason": "ordem ambígua sem resolução"})

    def _startup_reconcile(self) -> None:
        """After a restart: never-sent intents are cancelled; sent ones become UNKNOWN and are
        reconciled; any still unknown block every new entry until resolved."""
        now = self.market.now
        for it in self.intents.values():
            if it.state is OrderState.AUTHORIZED:  # SENT is written before the venue sees it
                it.move(OrderState.CANCELLED, now, "não enviada antes do reinício")
                if it.is_entry:
                    self.risk.release(it.intent_id)
            elif it.state is OrderState.SENT:
                self._mark_unknown(it, "resultado desconhecido após reinício")
        for it in list(self.intents.values()):
            if it.state is OrderState.UNKNOWN:
                self._reconcile_intent(it, recovered=True)
            elif it.state in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED):
                self._poll_open(it, recovered=True)
        self.startup_pending = {
            k for k, it in self.intents.items() if it.state is OrderState.UNKNOWN
        }

    def _poll_open(self, it: Intent, recovered: bool = False) -> None:
        outcome, status = self._lookup(it.client_order_id)
        if outcome is LookupOutcome.FOUND and status is not None:
            self._apply_status(it, status, recovered=recovered)

    def reconcile_ambiguous(self) -> bool:
        """Retries every UNKNOWN order (bounded) and refreshes working orders. Returns True when
        something changed."""
        changed = False
        for it in list(self.intents.values()):
            if it.state is OrderState.UNKNOWN and not it.reconcile_exhausted:
                resolved = self._reconcile_intent(it)
                changed = changed or resolved or it.reconcile_exhausted
            elif it.state in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED):
                before = (it.state, it.filled_units)
                self._poll_open(it)
                changed = changed or (it.state, it.filled_units) != before
        self.startup_pending = {
            k for k in self.startup_pending if self.intents[k].state is OrderState.UNKNOWN
        }
        if changed:
            self.broker.collect_fills()
        return changed

    def ambiguous_pairs(self) -> dict[str, str]:
        return {
            it.pair: it.client_order_id
            for it in self.intents.values()
            if it.state is OrderState.UNKNOWN
        }

    def entry_block(self) -> str | None:
        if self.startup_pending:
            return (
                f"reconciliação de arranque pendente: {len(self.startup_pending)} ordem(ns) "
                "com resultado desconhecido"
            )
        return None

    # --- events / persistence -------------------------------------------------------------

    def journal(self, kind: str, payload: dict[str, Any]) -> None:
        ts = self.market.now if hasattr(self, "market") else datetime.now(UTC)
        seq = self.store.append(kind, payload, ts)
        event = {"seq": seq, "ts": ts.isoformat(), "kind": kind, "payload": payload}
        for fn in list(self.listeners):
            fn(event)

    def save(self) -> None:
        self.store.put_states(
            {
                "market": self.market.to_dict(),
                "broker": self.broker.to_dict(),
                "risk": self.risk.state.to_dict(),
                "orchestrator": {
                    "version": 2,
                    "intents": self._intents_to_keep(),
                    "owners": self.owners,
                    "cycles": self.cycles,
                    "equity_curve": self.equity_curve[-2000:],
                    "journal_head": dict(zip(("seq", "hash"), self.store.head(), strict=True)),
                    "pause": {
                        "paused": self.paused,
                        "reason": self.pause_reason,
                        "detail": self.pause_detail,
                        "at": self.paused_at,
                    },
                },
            }
        )

    def _intents_to_keep(self) -> dict[str, Any]:
        """The last 400 intents plus every non-terminal one (never drop an open order)."""
        items = list(self.intents.items())
        keep = dict(items[-400:])
        for k, it in items[:-400]:
            if it.state not in TERMINAL:
                keep[k] = it
        return {k: v.to_dict() for k, v in keep.items()}

    # --- pause (M5) ------------------------------------------------------------------------

    def pause(self, reason: str, detail: str = "") -> None:
        """`manual` (operator) or `safety` (internal error). Persisted: a restart keeps it."""
        self.paused = True
        self.pause_reason = reason
        self.pause_detail = detail
        self.paused_at = datetime.now(UTC).isoformat()
        self.journal("engine_paused", {"reason": reason, "detail": detail})
        self.save()

    def resume(self) -> None:
        """Only an explicit operator command resumes; locks and the kill switch are untouched."""
        if not self.paused:
            return
        previous = self.pause_reason
        self.paused, self.pause_reason, self.pause_detail, self.paused_at = False, None, "", None
        self.journal("engine_resumed", {"previous_reason": previous})
        self.save()

    # --- main loop -------------------------------------------------------------------------

    def tick(self) -> None:
        """One simulated minute."""
        if self.paused:
            return
        quotes, closed = self.market.step()
        now = self.market.now
        self.ticks += 1
        changed = self.reconcile_ambiguous()
        for f in self.broker.on_quotes(quotes):
            self._on_venue_fill(f)
            changed = True
        self.broker.collect_fills()
        acct = self.broker.account()
        for ev in self.risk.observe(acct.equity, now):
            changed = True
            if ev == "day_rolled":
                self.journal(
                    "trading_day_started",
                    {"day": self.risk.state.trading_day, "equity": acct.equity},
                )
            else:
                self.journal(
                    "risk_lock",
                    {"lock": self.risk.state.lock, "pnl_pct": self.risk.state.lock_pnl_pct},
                )
        if self.risk.should_flatten() and self.broker.positions():
            self.flatten_all("bloqueio diário: fecho de posições")
            changed = True
        if now.minute % 15 == 0:
            self.equity_curve.append([now.isoformat(), round(acct.equity, 2)])
        decision_pairs = [p for p, tf, _ in closed if tf == self.config.timeframe]
        if decision_pairs:
            before = len(self.intents)
            self.run_cycle(decision_pairs)
            changed = changed or len(self.intents) != before
        if changed or self.ticks % SAVE_EVERY_TICKS == 0:
            self.save()

    def run_cycle(self, pairs: list[str]) -> list[PairDecision]:
        now = self.market.now
        self.cycles += 1
        positions = {p.pair: p for p in self.broker.positions()}
        decisions: list[PairDecision] = []
        for sym in pairs:
            bars = self.market.bars(sym, self.config.timeframe, self.config.history_bars)
            q = self.market.latest(sym)
            if q is None or not bars:
                continue
            units = positions[sym].units if sym in positions else 0.0
            view = MarketView(sym, self.config.timeframe, bars, now, units, q.spread)
            regime = self.regime_agent.analyze(view)
            props = [a.analyze(view) for a in self.agents]
            self.last_proposals[sym] = {p.agent.value: p.to_dict() for p in [regime, *props]}
            owner = self.owners.get(sym)
            d = self.coordinator.decide_pair(
                sym, regime, props, units, AgentRole(owner) if owner else None
            )
            self.last_decisions[sym] = d.to_dict()
            decisions.append(d)
            for p in [regime, *props]:
                self._agent_seen(p)
        exposure = self.broker.book.currency_exposure(self.market.quotes)
        chosen = self.coordinator.rank(decisions, exposure)
        self.agent_status[AgentRole.COORDINATOR.value] = {
            "last_run": now.isoformat(),
            "pairs": len(decisions),
            "selected": [d.pair for d in chosen],
            "summary": "; ".join(f"{d.pair}: {d.summary}" for d in chosen) or "sem ações",
        }
        self.journal(
            "cycle",
            {
                "cycle": self.cycles,
                "pairs": len(decisions),
                # HOLD decisions are not journaled (they are in every snapshot); signals are
                "decisions": [
                    d.to_dict() for d in decisions if d.action is not ProposalAction.HOLD
                ],
                "selected": [d.pair for d in chosen],
            },
        )
        for d in chosen:
            if d.request is not None:
                lead = d.lead_agent.value if d.lead_agent else None
                self.process(
                    d.request,
                    lead,
                    {
                        p: self.last_proposals[d.pair].get(p)
                        for p in self.last_proposals.get(d.pair, {})
                    },
                )
        return decisions

    def _agent_seen(self, p: AgentProposal) -> None:
        st = self.agent_status.setdefault(p.agent.value, {"signals": 0, "last_run": None})
        st["last_run"] = p.created_at.isoformat()
        if p.action in (ProposalAction.OPEN, ProposalAction.CLOSE, ProposalAction.VETO):
            st["signals"] = int(st.get("signals", 0)) + 1
            st["last_signal"] = {
                "pair": p.pair,
                "action": p.action.value,
                "direction": p.direction.value,
                "score": p.score,
                "rationale": p.rationale,
            }

    # --- execution -------------------------------------------------------------------------

    def process(
        self, req: TradeRequest, lead_agent: str | None, proposals: dict[str, Any] | None = None
    ) -> Intent | None:
        now = self.market.now
        iid = intent_id_for(req.proposal_id)
        if iid in self.intents:
            return self.intents[iid]  # idempotent: one intent per proposal
        decision = self.risk.evaluate(
            req,
            quotes=self.market.quotes,
            account=self.broker.account(),
            positions={p.pair: p for p in self.broker.positions()},
            now=now,
            currency=self.config.account_currency,
            venue_leverage=self.broker.capabilities.max_leverage,
            ambiguous_pairs=self.ambiguous_pairs(),
            entry_block=self.entry_block(),
        )
        side = decision.side or (Side.BUY if req.direction is Direction.LONG else Side.SELL)
        it = Intent(
            iid,
            req.proposal_id,
            req.pair,
            side.value,
            decision.units,
            decision.is_entry,
            OrderState.PROPOSAL,
            now.isoformat(),
            f"fx-{iid[4:]}",
            lead_agent,
            req.stop_loss,
            req.take_profit,
        )
        it.history.append([OrderState.SIGNAL.value, now.isoformat(), "sinal dos agentes"])
        it.history.append([OrderState.PROPOSAL.value, now.isoformat(), "proposta do coordenador"])
        self.intents[iid] = it
        self.journal(
            "risk_decision",
            {
                "intent_id": iid,
                "request": req.__dict__ | {"direction": req.direction.value},
                "decision": decision.to_dict(),
                "proposals": proposals or {},
            },
        )
        if not decision.approved:
            it.move(OrderState.REFUSED, now, decision.summary)
            it.reason = decision.summary
            return it
        it.move(OrderState.AUTHORIZED, now, decision.summary)
        if decision.is_entry:
            self.risk.reserve(iid, req.pair)
        self._send(it, decision)
        return it

    def _send(self, it: Intent, decision: RiskDecision) -> None:
        now = self.market.now
        if decision.side is None:
            raise RuntimeError("an approved risk decision must carry a side")
        request = OrderRequest(
            it.client_order_id,
            it.intent_id,
            it.pair,
            decision.side,
            decision.units,
            now,
            reduce_only=not decision.is_entry,
            stop_loss=it.stop_loss if decision.is_entry else None,
            take_profit=it.take_profit if decision.is_entry else None,
        )
        require_executable(self.environment)
        it.move(OrderState.SENT, now, "ordem enviada ao corretor")
        self.journal("order_sent", {"intent_id": it.intent_id, "order": request.to_dict()})
        self.save()  # write-ahead: the intent is SENT on disk before the venue sees it
        try:
            status = self.broker.submit(request)
        except ExecutionError as exc:
            if not exc.ambiguous:
                it.move(OrderState.REJECTED, now, str(exc))
                it.reason = str(exc)
                if it.is_entry:
                    self.risk.release(it.intent_id)
                self.journal(
                    "order_result",
                    {"intent_id": it.intent_id, "state": "rejected", "reason": str(exc)},
                )
                self.save()
                return
            self._mark_unknown(it, f"resposta ambígua: {exc}")
            self.journal("order_unknown", {"intent_id": it.intent_id, "reason": str(exc)})
            self._reconcile_intent(it)  # never a resend: look it up, bounded retries follow
            self.broker.collect_fills()
            self.save()
            return
        self._apply_status(it, status)
        self.broker.collect_fills()
        self.save()  # the outcome is durable before anything else happens

    def _apply_status(self, it: Intent, status: OrderStatus, recovered: bool = False) -> None:
        """Brings an intent to the venue's reported status (M6). `filled_units` is the
        venue's cumulative total, never added to: repeated reports cannot double-count, and the
        entry is consumed once (a reservation is popped only once)."""
        now = self.market.now
        note = " (recuperado)" if recovered else ""
        st = status.state

        def to(target: OrderState, text: str) -> None:
            if it.state is not target:
                it.move(target, now, text + note)

        def accepted() -> None:  # the venue holds the order: only from SENT or UNKNOWN
            if it.state in (OrderState.SENT, OrderState.UNKNOWN):
                it.move(OrderState.ACCEPTED, now, "aceite pelo corretor" + note)

        if st is OrderState.REJECTED:
            to(OrderState.REJECTED, status.reason or "rejeitada pelo corretor")
            it.reason = status.reason
            if it.is_entry:
                self.risk.release(it.intent_id)
        elif st is OrderState.ACCEPTED:
            accepted()
        elif st in (
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        ):
            accepted()
            filled = status.filled_units
            if filled > it.filled_units + EPS_UNITS:
                progress = f"{filled:.0f} de {status.units:.0f} @ {status.avg_price}"
                if st is OrderState.FILLED:
                    it.move(OrderState.FILLED, now, progress + note)
                else:  # first or further partial fill
                    it.move(OrderState.PARTIALLY_FILLED, now, progress + note)
                it.filled_units, it.avg_price = filled, status.avg_price
                if it.is_entry:
                    self.risk.consume(it.intent_id)
                    if it.lead_agent:
                        self.owners[it.pair] = it.lead_agent
            elif st is OrderState.FILLED:
                to(OrderState.FILLED, "execução total")
            if st in (OrderState.CANCELLED, OrderState.EXPIRED):
                if filled > EPS_UNITS:
                    text = f"restante cancelado ({status.remaining_units:.0f} por executar)"
                else:
                    text = "cancelada pelo corretor sem execução"
                    if it.is_entry:
                        self.risk.release(it.intent_id)
                to(st, text)
                it.reason = status.reason or it.reason
            if self.broker.book.positions.get(it.pair) is None:
                self.owners.pop(it.pair, None)
        self.journal(
            "order_result",
            {"intent_id": it.intent_id, "state": it.state.value, "status": status.to_dict()},
        )

    def _on_venue_fill(self, f: Fill) -> None:
        """A fill initiated by the venue (stop loss, take profit, margin close-out)."""
        if f.pair not in self.broker.book.positions:
            self.owners.pop(f.pair, None)
        self.journal("position_closed", {"reason": f.reason, "fill": f.to_dict()})

    def flatten_all(self, reason: str) -> list[Intent]:
        out = []
        for p in list(self.broker.positions()):
            pid = f"flatten-{p.position_id}-{self.market.now.isoformat()}"
            req = TradeRequest(p.pair, Direction.FLAT, None, None, 1.0, pid)
            it = self.process(req, None, {"reason": reason})
            if it is not None:
                out.append(it)
        self.journal("flatten_all", {"reason": reason, "orders": len(out)})
        self.save()
        return out

    # --- commands --------------------------------------------------------------------------

    def engage_kill_switch(self, reason: str) -> None:
        self.risk.engage_kill_switch(reason or "manual", self.market.now)
        self.journal("kill_switch", {"engaged": True, "reason": reason})
        self.save()

    def release_kill_switch(self, confirmation: str) -> None:
        self.risk.release_kill_switch(confirmation)
        self.journal("kill_switch", {"engaged": False})
        self.save()

    def apply_config(self, cfg: FxConfig) -> None:
        """Risk and cycle settings apply now; engaged locks and the kill switch stay as they
        are. Account currency, capital, pairs and seed need an account reset."""
        require_executable(cfg.environment)
        if cfg.environment is not self.environment:
            raise EnvironmentMismatchError("o ambiente não pode mudar com o motor a correr")
        if cfg.account_currency != self.broker.book.currency:
            raise AccountMismatchError("a moeda da conta só muda com uma reposição explícita")
        self.config = cfg
        self.risk.config = cfg.risk.to_risk_config()
        self.coordinator = PortfolioCoordinator(cfg.risk.min_score, cfg.max_new_entries_per_cycle)
        self.journal("config_changed", {"config": cfg.model_dump(mode="json")})
        self.save()

    # --- snapshot for the UI ---------------------------------------------------------------

    def snapshot(self, recent_events: int = 40) -> dict[str, Any]:
        now = self.market.now
        acct = self.broker.account()
        quotes = {
            s: {
                "bid": q.bid,
                "ask": q.ask,
                "mid": round(q.mid, 6),
                "spread_pips": round(q.spread / (0.01 if s.endswith("JPY") else 0.0001), 2),
                "ts": q.ts.isoformat(),
                "origin": q.origin.value,
                "source": q.source,
            }
            for s, q in self.market.quotes.items()
        }
        conv_q = self.market.quotes
        positions = []
        for p in self.broker.positions():
            q = conv_q.get(p.pair)
            mark = q.mark_for(p.units) if q else p.avg_price
            positions.append({**p.to_dict(), "mark": mark, "owner": self.owners.get(p.pair)})
        open_intents = [i.to_dict() for i in self.intents.values() if i.state not in TERMINAL]
        recent_intents = [i.to_dict() for i in list(self.intents.values())[-25:]]
        return {
            "engine_version": ENGINE_VERSION,
            "environment": self.environment.value,
            "environment_label": ENV_LABELS[self.environment],
            "data_origin": self.market.origin.value,
            "now": now.isoformat(),
            "trading_day": self.calendar.trading_day(now).isoformat(),
            "market_open": self.calendar.is_open(now),
            "sessions": self.calendar.active_sessions(now),
            "next_rollover": self.calendar.next_close(now).isoformat(),
            "paused": self.paused,
            "pause_reason": self.pause_reason,
            "pause_detail": self.pause_detail,
            "environment_validated": True,  # the engine cannot run otherwise (E2)
            "adapter": self.broker.capabilities.description,
            "ambiguous_orders": len(self.ambiguous_pairs()),
            "entry_block": self.entry_block(),
            "speed": self.config.simulation.steps_per_second,
            "recovered": self.recovered,
            "cycles": self.cycles,
            "timeframe": self.config.timeframe,
            "account": acct.to_dict(),
            "initial_capital": self.config.initial_capital,
            "realized_total": self.broker.book.realized,
            "commissions_total": self.broker.book.commissions,
            "quotes": quotes,
            "positions": positions,
            "exposure": self.broker.book.currency_exposure(conv_q),
            "risk": self.risk.status(),
            "agents": self.agent_status,
            "proposals": self.last_proposals,
            "decisions": self.last_decisions,
            "open_intents": open_intents,
            "recent_intents": recent_intents,
            "closed_positions": self.broker.closed_positions[-20:],
            "equity_curve": self.equity_curve[-300:],
            "charts": {
                s: [round(b.close, 6) for b in self.market.bars(s, self.config.timeframe, 96)]
                for s in self.market.pairs
            },
            "events": [compact_event(e) for e in self.store.recent(recent_events, UI_EVENT_KINDS)],
            "config": self.config.model_dump(mode="json"),
        }
