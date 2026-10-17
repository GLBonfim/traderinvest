"""Shared agent plumbing: bar frames, ATR, ids and the proposal factory.

Agents are pure functions of closed bars: they read bars up to and including the last CLOSED
bar only (no look-ahead) and they never touch the broker, the risk state or the network.
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from typing import Any, Protocol

import pandas as pd

from app.fx.models import Bar, Direction, pair
from app.fx.proposals import AgentProposal, AgentRole, ProposalAction
from app.indicators.indicators import true_range, wilder


@dataclass(frozen=True)
class MarketView:
    """What an agent may see for one pair at decision time."""

    pair: str
    timeframe: str
    bars: Sequence[Bar]  # closed bars, oldest first
    now: datetime
    position_units: float  # signed current position (0 = flat)
    spread: float  # current spread in price units

    @cached_property
    def df(self) -> pd.DataFrame:
        """The bars as a frame, built once and shared by every agent of the cycle."""
        return frame(self.bars)

    @cached_property
    def atr14(self) -> pd.Series:
        return atr(self.df)


class Agent(Protocol):
    role: AgentRole
    version: str
    min_bars: int

    def analyze(self, view: MarketView) -> AgentProposal: ...


def frame(bars: Sequence[Bar]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
        },
        index=pd.DatetimeIndex([b.ts for b in bars]),
    )


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return wilder(true_range(df["high"], df["low"], df["close"]), n)


def proposal_id(role: AgentRole, symbol: str, ts: datetime) -> str:
    digest = hashlib.sha256(f"{role.value}|{symbol}|{ts.isoformat()}".encode()).hexdigest()
    return f"{role.value[:3]}-{symbol}-{digest[:10]}"


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def r(x: float, symbol: str) -> float:
    """Rounds a price to the pair's display precision."""
    return round(x, 3 if pair(symbol).pip_size >= 0.01 else 5)


def make(
    agent: Agent,
    view: MarketView,
    action: ProposalAction,
    direction: Direction,
    score: float,
    rationale: str,
    evidence: dict[str, Any],
    entry: float | None = None,
    stop: float | None = None,
    target: float | None = None,
) -> AgentProposal:
    last = view.bars[-1]
    rr = None
    if entry is not None and stop is not None and target is not None and entry != stop:
        rr = round(abs(target - entry) / abs(entry - stop), 2)
    return AgentProposal(
        proposal_id=proposal_id(agent.role, view.pair, last.ts),
        agent=agent.role,
        agent_version=agent.version,
        pair=view.pair,
        timeframe=view.timeframe,
        created_at=view.now,
        action=action,
        direction=direction,
        score=round(clip01(score), 4),
        entry_price=None if entry is None else r(entry, view.pair),
        stop_loss=None if stop is None else r(stop, view.pair),
        take_profit=None if target is None else r(target, view.pair),
        risk_reward=rr,
        rationale=rationale,
        evidence={k: (round(v, 6) if isinstance(v, float) else v) for k, v in evidence.items()},
        data_origin=last.origin.value,
    )


def hold(agent: Agent, view: MarketView, rationale: str, evidence: dict[str, Any]) -> AgentProposal:
    return make(agent, view, ProposalAction.HOLD, Direction.FLAT, 0.0, rationale, evidence)
