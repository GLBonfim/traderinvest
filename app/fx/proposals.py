"""Agent proposals: what an agent recommends, with its evidence. A proposal is NOT an order.

`score` is a heuristic confidence in [0, 1] built from indicator agreement. It is NOT a
probability of profit and must never be presented as one (ADR-0036).
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.fx.models import Direction, utc


class AgentRole(StrEnum):
    TREND = "trend"
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    REGIME = "regime"
    COORDINATOR = "coordinator"


class ProposalAction(StrEnum):
    OPEN = "open"  # open (or reverse into) a position in `direction`
    CLOSE = "close"  # close the existing position
    HOLD = "hold"  # no change recommended
    VETO = "veto"  # filter agents only: no new entries for this pair now


@dataclass(frozen=True)
class AgentProposal:
    proposal_id: str
    agent: AgentRole
    agent_version: str
    pair: str
    timeframe: str
    created_at: datetime
    action: ProposalAction
    direction: Direction
    score: float  # heuristic confidence in [0, 1]; NOT a probability
    entry_price: float | None  # reference price at analysis time (mid)
    stop_loss: float | None
    take_profit: float | None
    risk_reward: float | None
    rationale: str  # short human-readable reason (Portuguese UI text)
    evidence: dict[str, Any] = field(default_factory=dict)  # indicator values used
    data_origin: str = "synthetic"
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score must be in [0, 1]")
        object.__setattr__(self, "created_at", utc(self.created_at))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", utc(self.expires_at))
        if self.action is ProposalAction.OPEN:
            if self.direction is Direction.FLAT:
                raise ValueError("an OPEN proposal needs a LONG or SHORT direction")
            if self.stop_loss is None:
                raise ValueError("an OPEN proposal must carry a stop loss")
            ref = self.entry_price
            if ref is not None:
                wrong = (self.direction is Direction.LONG and self.stop_loss >= ref) or (
                    self.direction is Direction.SHORT and self.stop_loss <= ref
                )
                if wrong:
                    raise ValueError("the stop loss is on the wrong side of the entry price")

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "agent": self.agent.value,
            "agent_version": self.agent_version,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "created_at": self.created_at.isoformat(),
            "action": self.action.value,
            "direction": self.direction.value,
            "score": round(self.score, 4),
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "risk_reward": self.risk_reward,
            "rationale": self.rationale,
            "evidence": self.evidence,
            "data_origin": self.data_origin,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }
