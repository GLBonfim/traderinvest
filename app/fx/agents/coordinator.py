"""Portfolio Coordinator v2: a desk head, not a voting machine.

Each specialist trades the setup it was validated on; the coordinator decides which of them may
act, and how the portfolio is protected. It authorizes nothing: every request still goes to the
central risk manager.

Per pair:
1. an open position is closed only when the agent that opened it asks (its time stop); stops and
   targets are held by the venue. (v1 also closed on an "opposite consensus", a behaviour that
   was never validated, so v2 does not.)
2. a regime VETO blocks new entries (exits stay possible);
3. only APPROVED agents (passed the pre-registered out-of-sample rule, app.fx.agents.calibrated)
   may open; other agents' signals are shown as "em observação";
4. opposite signals on the same pair = conflict -> no trade;
5. otherwise the strongest approved signal leads (its stop and target are used).

Across pairs: exits first; then entries ordered by score, those adding to an already crowded
same-direction currency exposure placed after the others (diversification). Scores are never
lowered (a lowered score could fall under the risk manager's minimum); at most
`max_new_entries_per_cycle` entries per cycle.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.fx.agents import calibrated
from app.fx.agents.base import proposal_id
from app.fx.agents.regime import MIXED
from app.fx.models import Direction, pair
from app.fx.proposals import AgentProposal, AgentRole, ProposalAction
from app.fx.risk import TradeRequest

AGENT_LABEL = {
    AgentRole.TREND: "tendência",
    AgentRole.MOMENTUM: "momentum",
    AgentRole.MEAN_REVERSION: "reversão",
}


@dataclass
class PairDecision:
    pair: str
    action: ProposalAction
    direction: Direction
    score: float
    regime: str
    summary: str
    lead_agent: AgentRole | None = None
    request: TradeRequest | None = None
    contributions: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "action": self.action.value,
            "direction": self.direction.value,
            "score": round(self.score, 4),
            "regime": self.regime,
            "summary": self.summary,
            "lead_agent": None if self.lead_agent is None else self.lead_agent.value,
            "contributions": self.contributions,
        }


class PortfolioCoordinator:
    role = AgentRole.COORDINATOR
    version = "2.0.0"

    def __init__(
        self,
        entry_threshold: float = 0.55,
        max_new_entries_per_cycle: int = 1,
        approved: frozenset[str] | None = None,
    ) -> None:
        self.entry_threshold = entry_threshold
        self.max_new_entries_per_cycle = max_new_entries_per_cycle
        self.approved = calibrated.APPROVED if approved is None else approved

    def decide_pair(
        self,
        symbol: str,
        regime: AgentProposal,
        proposals: Sequence[AgentProposal],
        position_units: float,
        owner: AgentRole | None,
    ) -> PairDecision:
        reg = str(regime.evidence.get("regime", MIXED))
        contrib = {
            p.agent.value: {
                "action": p.action.value,
                "direction": p.direction.value,
                "score": p.score,
                "approved": p.agent.value in self.approved,
            }
            for p in proposals
        }
        ts = regime.created_at
        if abs(position_units) > 0:
            held = Direction.LONG if position_units > 0 else Direction.SHORT
            owner_close = owner is not None and any(
                p.agent is owner and p.action is ProposalAction.CLOSE for p in proposals
            )
            if owner_close:
                req = TradeRequest(
                    symbol, Direction.FLAT, None, None, 1.0, self._pid(symbol, ts, "close")
                )
                return PairDecision(
                    symbol,
                    ProposalAction.CLOSE,
                    Direction.FLAT,
                    1.0,
                    reg,
                    "fechar: tempo máximo da operação (agente que a abriu)",
                    owner,
                    req,
                    contrib,
                )
            return PairDecision(
                symbol, ProposalAction.HOLD, held, 0.0, reg, "manter posição", owner, None, contrib
            )
        if regime.action is ProposalAction.VETO:
            return PairDecision(
                symbol,
                ProposalAction.VETO,
                Direction.FLAT,
                0.0,
                reg,
                f"vetado: {regime.rationale}",
                None,
                None,
                contrib,
            )
        opens = [p for p in proposals if p.action is ProposalAction.OPEN]
        candidates = [
            p for p in opens if p.agent.value in self.approved and p.score >= self.entry_threshold
        ]
        if {p.direction for p in candidates} >= {Direction.LONG, Direction.SHORT}:
            return PairDecision(
                symbol,
                ProposalAction.HOLD,
                Direction.FLAT,
                0.0,
                reg,
                "conflito: sinais opostos de agentes aprovados",
                None,
                None,
                contrib,
            )
        if not candidates:
            watched = [AGENT_LABEL.get(p.agent, p.agent.value) for p in opens]
            why = (
                f"sinal em observação ({', '.join(watched)}: agente não aprovado)"
                if watched
                else "sem sinal"
            )
            return PairDecision(
                symbol, ProposalAction.HOLD, Direction.FLAT, 0.0, reg, why, None, None, contrib
            )
        lead = max(candidates, key=lambda p: p.score)
        req = TradeRequest(
            symbol,
            lead.direction,
            lead.stop_loss,
            lead.take_profit,
            lead.score,
            self._pid(symbol, ts, lead.direction.value),
        )
        label = "compra" if lead.direction is Direction.LONG else "venda"
        return PairDecision(
            symbol,
            ProposalAction.OPEN,
            lead.direction,
            lead.score,
            reg,
            f"{label} ({AGENT_LABEL.get(lead.agent, lead.agent.value)})",
            lead.agent,
            req,
            contrib,
        )

    def rank(
        self, decisions: Sequence[PairDecision], exposure: Mapping[str, float]
    ) -> list[PairDecision]:
        """Exits first, then entries by score with crowded currencies last, capped per cycle."""
        exits = [d for d in decisions if d.action is ProposalAction.CLOSE]
        entries = []
        for d in decisions:
            if d.action is not ProposalAction.OPEN or d.request is None:
                continue
            p = pair(d.pair)
            sign = 1 if d.direction is Direction.LONG else -1
            crowded = exposure.get(p.base, 0.0) * sign > 0 or exposure.get(p.quote, 0.0) * sign < 0
            if crowded:
                d.summary += " (exposição já concentrada nesta moeda)"
            entries.append((crowded, -d.score, d))
        entries.sort(key=lambda t: (t[0], t[1]))
        return exits + [d for _, _, d in entries[: self.max_new_entries_per_cycle]]

    @staticmethod
    def _pid(symbol: str, ts: datetime, tag: str) -> str:
        return proposal_id(AgentRole.COORDINATOR, f"{symbol}-{tag}", ts)
