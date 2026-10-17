"""Portfolio Coordinator agent: turns the specialists' proposals into at most one trade request
per pair, and ranks pairs across the portfolio. It does NOT authorize anything: every request
still goes to the central risk manager.

Per pair:
1. A regime VETO blocks new entries (exits stay possible).
2. Agent weights depend on the regime (trend-friendly in TRENDING, mean-reversion-friendly in
   RANGING, neutral otherwise).
3. For each direction: coverage = agreeing weight / total weight; quality = weighted mean score
   of the agreeing agents; score = quality x (0.5 + 0.5 x coverage). Both directions supported
   at once is a conflict -> HOLD.
4. Stop / target come from the strongest agreeing agent (weight x score).
5. An open position is closed when the agent that opened it proposes CLOSE, or when the
   opposite direction reaches the entry threshold.

Across pairs: candidates adding to an existing same-direction currency exposure are penalised
(x 0.85, diversification), then ranked; only `max_new_entries_per_cycle` are forwarded.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.fx.agents.base import proposal_id
from app.fx.agents.regime import MIXED, QUIET, RANGING, TRENDING, VOLATILE
from app.fx.models import Direction, pair
from app.fx.proposals import AgentProposal, AgentRole, ProposalAction
from app.fx.risk import TradeRequest

WEIGHTS: dict[str, dict[AgentRole, float]] = {
    TRENDING: {AgentRole.TREND: 1.0, AgentRole.MOMENTUM: 0.8, AgentRole.MEAN_REVERSION: 0.2},
    RANGING: {AgentRole.TREND: 0.2, AgentRole.MOMENTUM: 0.4, AgentRole.MEAN_REVERSION: 1.0},
    MIXED: {AgentRole.TREND: 0.6, AgentRole.MOMENTUM: 0.6, AgentRole.MEAN_REVERSION: 0.6},
}
WEIGHTS[VOLATILE] = WEIGHTS[MIXED]
WEIGHTS[QUIET] = WEIGHTS[MIXED]


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
    version = "1.0.0"

    def __init__(self, entry_threshold: float = 0.55, max_new_entries_per_cycle: int = 1) -> None:
        self.entry_threshold = entry_threshold
        self.max_new_entries_per_cycle = max_new_entries_per_cycle

    def decide_pair(
        self,
        symbol: str,
        regime: AgentProposal,
        proposals: Sequence[AgentProposal],
        position_units: float,
        owner: AgentRole | None,
    ) -> PairDecision:
        reg = str(regime.evidence.get("regime", MIXED))
        w = WEIGHTS.get(reg, WEIGHTS[MIXED])
        total = sum(w.get(p.agent, 0.0) for p in proposals) or 1.0
        contrib = {
            p.agent.value: {
                "action": p.action.value,
                "direction": p.direction.value,
                "score": p.score,
                "weight": w.get(p.agent, 0.0),
            }
            for p in proposals
        }
        side_stats: dict[Direction, tuple[float, float, AgentProposal | None]] = {}
        for d in (Direction.LONG, Direction.SHORT):
            agree = [p for p in proposals if p.action is ProposalAction.OPEN and p.direction is d]
            aw = sum(w.get(p.agent, 0.0) for p in agree)
            quality = sum(w.get(p.agent, 0.0) * p.score for p in agree) / aw if aw else 0.0
            score = quality * (0.5 + 0.5 * aw / total)
            lead = max(agree, key=lambda p: w.get(p.agent, 0.0) * p.score, default=None)
            side_stats[d] = (score, aw / total, lead)

        # exits first
        if abs(position_units) > 0:
            held = Direction.LONG if position_units > 0 else Direction.SHORT
            opposite = Direction.SHORT if held is Direction.LONG else Direction.LONG
            owner_close = owner is not None and any(
                p.agent is owner and p.action is ProposalAction.CLOSE for p in proposals
            )
            opp_score = side_stats[opposite][0]
            if owner_close or opp_score >= self.entry_threshold:
                why = (
                    "o agente que abriu a posição pede o fecho"
                    if owner_close
                    else f"consenso contrário ({opp_score:.2f})"
                )
                req = TradeRequest(
                    symbol,
                    Direction.FLAT,
                    None,
                    None,
                    1.0,
                    self._pid(symbol, regime.created_at, "close"),
                )
                return PairDecision(
                    symbol,
                    ProposalAction.CLOSE,
                    Direction.FLAT,
                    1.0,
                    reg,
                    f"fechar: {why}",
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
                f"vetado pelo filtro de regime: {regime.rationale}",
                None,
                None,
                contrib,
            )
        long_s, short_s = side_stats[Direction.LONG][0], side_stats[Direction.SHORT][0]
        if long_s > 0.15 and short_s > 0.15:
            return PairDecision(
                symbol,
                ProposalAction.HOLD,
                Direction.FLAT,
                0.0,
                reg,
                "conflito entre agentes",
                None,
                None,
                contrib,
            )
        d = Direction.LONG if long_s >= short_s else Direction.SHORT
        score, coverage, lead = side_stats[d]
        if lead is None or score < self.entry_threshold:
            return PairDecision(
                symbol,
                ProposalAction.HOLD,
                Direction.FLAT,
                score,
                reg,
                "sem consenso suficiente",
                None,
                None,
                contrib,
            )
        req = TradeRequest(
            symbol,
            d,
            lead.stop_loss,
            lead.take_profit,
            score,
            self._pid(symbol, regime.created_at, d.value),
        )
        label = "compra" if d is Direction.LONG else "venda"
        return PairDecision(
            symbol,
            ProposalAction.OPEN,
            d,
            score,
            reg,
            f"{label} proposta (líder: {lead.agent.value}, cobertura {coverage:.0%})",
            lead.agent,
            req,
            contrib,
        )

    def rank(
        self, decisions: Sequence[PairDecision], exposure: Mapping[str, float]
    ) -> list[PairDecision]:
        """Exits first, then the best entries (diversification-adjusted), capped per cycle."""
        exits = [d for d in decisions if d.action is ProposalAction.CLOSE]
        entries = []
        for d in decisions:
            if d.action is not ProposalAction.OPEN or d.request is None:
                continue
            p = pair(d.pair)
            sign = 1 if d.direction is Direction.LONG else -1
            crowded = exposure.get(p.base, 0.0) * sign > 0 or exposure.get(p.quote, 0.0) * sign < 0
            adj = d.score * (0.85 if crowded else 1.0)
            entries.append((adj, d))
        entries.sort(key=lambda t: t[0], reverse=True)
        chosen = []
        for adj, d in entries[: self.max_new_entries_per_cycle]:
            if d.request is not None and adj != d.score:
                d.request = TradeRequest(
                    d.request.pair,
                    d.request.direction,
                    d.request.stop_loss,
                    d.request.take_profit,
                    adj,
                    d.request.proposal_id,
                )
                d.summary += " (penalizado: exposição já concentrada)"
            chosen.append(d)
        return exits + chosen

    @staticmethod
    def _pid(symbol: str, ts: datetime, tag: str) -> str:
        return proposal_id(AgentRole.COORDINATOR, f"{symbol}-{tag}", ts)
