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
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from app.fx.environment import require_executable
from app.fx.lifecycle import LABELS as STATE_LABELS
from app.fx.lifecycle import TERMINAL, OrderState
from app.fx.models import Direction, Side
from app.fx.orders import ExecutionError, Fill, OrderRequest, OrderStatus
from app.fx.proposals import AgentProposal, AgentRole, ProposalAction
from app.fx.risk import RiskDecision, RiskManager, RiskState, TradeRequest
from app.fx.sim.broker import SimBroker, SimBrokerConfig
from app.fx.sim.market import SyntheticMarket
from app.fx.store import FxStore

ENGINE_VERSION = "0.1.0"
SAVE_EVERY_TICKS = 30
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
)


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

    def move(self, state: OrderState, ts: datetime, note: str = "") -> None:
        self.state = state
        self.history.append([state.value, ts.isoformat(), note])

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "state": self.state.value, "state_label": STATE_LABELS[self.state]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Intent":
        d = {k: v for k, v in d.items() if k != "state_label"}
        d["state"] = OrderState(d["state"])
        return cls(**d)


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
    def __init__(self, config: FxConfig, store: FxStore, now: datetime | None = None) -> None:
        self.config = config
        self.store = store
        self.environment = require_executable(config.environment)
        self.calendar = FxCalendar()
        self.listeners: list[Listener] = []
        self.paused = False
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
        market_d, broker_d = st.get_state("market"), st.get_state("broker")
        risk_d, orch_d = st.get_state("risk"), st.get_state("orchestrator")
        self.broker = SimBroker(self._broker_config())
        if market_d and broker_d and risk_d and orch_d:
            self.market = SyntheticMarket.from_dict(market_d, self.calendar)
            self.broker.load(broker_d)
            for f in self.broker.on_quotes(self.market.quotes):  # normally none: saved together
                self._on_venue_fill(f)
            self.broker.collect_fills()
            self.risk = RiskManager(
                self.config.risk.to_risk_config(),
                RiskState.from_dict(risk_d),
                self.calendar,
                self.environment,
            )
            self.intents = {k: Intent.from_dict(v) for k, v in orch_d["intents"].items()}
            self.owners = dict(orch_d.get("owners", {}))
            self.cycles = int(orch_d.get("cycles", 0))
            self.equity_curve = list(orch_d.get("equity_curve", []))
            self.recovered = True
            self.journal(
                "engine_recovered",
                {"intents": len(self.intents), "positions": len(self.broker.positions())},
            )
            self._resolve_open_intents()
        else:
            sim = self.config.simulation
            start = now - timedelta(minutes=sim.warm_up_minutes)
            self.market = SyntheticMarket(self.config.pairs, start, sim.seed, self.calendar)
            self.market.warm_up(sim.warm_up_minutes)
            self.broker.on_quotes(self.market.quotes)
            self.risk = RiskManager(
                self.config.risk.to_risk_config(), None, self.calendar, self.environment
            )
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
        self.risk.observe(self.broker.account().equity, self.market.now)
        self.save()

    def _resolve_open_intents(self) -> None:
        """After a restart: intents left SENT/UNKNOWN are resolved at the venue by id."""
        for it in self.intents.values():
            if it.state in (OrderState.SENT, OrderState.UNKNOWN, OrderState.AUTHORIZED):
                status = self.broker.order(it.client_order_id)
                if status is None:
                    if it.state is OrderState.AUTHORIZED:
                        it.move(
                            OrderState.CANCELLED, self.market.now, "não enviada antes do reinício"
                        )
                    else:
                        it.move(OrderState.UNKNOWN, self.market.now, "não encontrada no corretor")
                        it.move(OrderState.CANCELLED, self.market.now, "inexistente no corretor")
                    if it.is_entry:
                        self.risk.release(it.intent_id)
                else:
                    self._apply_status(it, status, recovered=True)

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
                    "version": 1,
                    "intents": {k: v.to_dict() for k, v in list(self.intents.items())[-400:]},
                    "owners": self.owners,
                    "cycles": self.cycles,
                    "equity_curve": self.equity_curve[-2000:],
                },
            }
        )

    # --- main loop -------------------------------------------------------------------------

    def tick(self) -> None:
        """One simulated minute."""
        if self.paused:
            return
        quotes, closed = self.market.step()
        now = self.market.now
        self.ticks += 1
        changed = False
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
            it.move(OrderState.UNKNOWN, now, f"resposta ambígua: {exc}")
            self.journal("order_unknown", {"intent_id": it.intent_id, "reason": str(exc)})
            found = self.broker.order(it.client_order_id)
            if found is None:
                it.move(OrderState.CANCELLED, now, "não existe no corretor")
                if it.is_entry:
                    self.risk.release(it.intent_id)
                self.save()
                return
            status = found
        self._apply_status(it, status)
        self.broker.collect_fills()
        self.save()  # the outcome is durable before anything else happens

    def _apply_status(self, it: Intent, status: OrderStatus, recovered: bool = False) -> None:
        now = self.market.now
        note = " (recuperado)" if recovered else ""
        if status.state is OrderState.REJECTED:
            it.move(OrderState.REJECTED, now, status.reason + note)
            it.reason = status.reason
            if it.is_entry:
                self.risk.release(it.intent_id)
        elif status.state in (OrderState.FILLED, OrderState.PARTIALLY_FILLED):
            if it.state is not OrderState.ACCEPTED:
                it.move(OrderState.ACCEPTED, now, "aceite pelo corretor" + note)
            it.move(status.state, now, f"{status.filled_units:.0f} @ {status.avg_price}" + note)
            it.filled_units, it.avg_price = status.filled_units, status.avg_price
            if it.is_entry:
                self.risk.consume(it.intent_id)
                if it.lead_agent:
                    self.owners[it.pair] = it.lead_agent
            pos = self.broker.book.positions.get(it.pair)
            if pos is None:
                self.owners.pop(it.pair, None)
        elif status.state is OrderState.ACCEPTED and it.state is not OrderState.ACCEPTED:
            it.move(OrderState.ACCEPTED, now, "aceite pelo corretor" + note)
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
