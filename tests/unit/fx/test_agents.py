"""The five agents on controlled bar series: direction, stops, vetoes, no look-ahead."""

import math

import pytest

from app.fx.agents import (
    MeanReversionAgent,
    MomentumAgent,
    PortfolioCoordinator,
    RegimeAgent,
    TrendAgent,
)
from app.fx.agents.base import MarketView
from app.fx.models import Direction
from app.fx.proposals import AgentProposal, AgentRole, ProposalAction
from tests.unit.fx.helpers import NOW, bars


def view(closes: list[float], units: float = 0.0, spread: float = 0.0001) -> MarketView:
    return MarketView("EURUSD", "M15", bars(closes), NOW, units, spread)


def trend_up(n: int = 120, step: float = 0.0004) -> list[float]:
    return [1.08 + step * i + 0.0003 * math.sin(i) for i in range(n)]


def ranging(n: int = 120) -> list[float]:
    return [1.08 + 0.002 * math.sin(i / 3) for i in range(n)]


def test_trend_agent_long_short_and_hold() -> None:
    up = TrendAgent().analyze(view(trend_up()))
    assert up.action is ProposalAction.OPEN and up.direction is Direction.LONG
    assert up.stop_loss is not None and up.entry_price is not None
    assert up.stop_loss < up.entry_price < (up.take_profit or 0)
    assert up.risk_reward == pytest.approx(1.5, abs=0.01)
    down = TrendAgent().analyze(view([2.16 - c for c in trend_up()]))
    assert down.direction is Direction.SHORT and down.stop_loss > down.entry_price  # type: ignore[operator]
    flat = TrendAgent().analyze(view(ranging()))
    assert flat.action is ProposalAction.HOLD
    short_data = TrendAgent().analyze(view(trend_up(30)))
    assert short_data.action is ProposalAction.HOLD and "insuficientes" in short_data.rationale


def test_trend_agent_closes_a_position_against_the_trend() -> None:
    p = TrendAgent().analyze(view(trend_up(), units=-10_000))
    assert p.action is ProposalAction.CLOSE


def test_mean_reversion_fades_an_excursion_and_targets_the_mean() -> None:
    closes = [*ranging(100), 1.0740, 1.0700, 1.0660]
    p = MeanReversionAgent().analyze(view(closes))
    assert p.action is ProposalAction.OPEN and p.direction is Direction.LONG
    assert p.take_profit is not None and p.take_profit > closes[-1]
    assert "evidence" not in p.rationale and p.evidence["rsi"] < 32


def test_momentum_agent_needs_agreement() -> None:
    accel = [1.08 + 0.00002 * i * i for i in range(80)]
    p = MomentumAgent().analyze(view(accel))
    assert p.action in (ProposalAction.OPEN, ProposalAction.HOLD)
    if p.action is ProposalAction.OPEN:
        assert p.direction is Direction.LONG
    assert MomentumAgent().analyze(view(ranging())).direction in (
        Direction.FLAT,
        Direction.LONG,
        Direction.SHORT,
    )
    assert set(p.evidence) >= {"rsi", "roc", "macd_hist"}


def test_regime_agent_classifies_and_vetoes() -> None:
    t = RegimeAgent().analyze(view(trend_up(240, 0.0006)))
    assert t.evidence["regime"] == "trending" and t.action is ProposalAction.HOLD
    r = RegimeAgent().analyze(view(ranging(240)))
    assert r.evidence["regime"] == "ranging"
    wide = RegimeAgent().analyze(view(ranging(240), spread=0.01))
    assert wide.action is ProposalAction.VETO
    spike = [*ranging(239), 1.10]
    v = RegimeAgent().analyze(view(spike))
    assert v.action is ProposalAction.VETO and v.evidence["regime"] == "volatile"


@pytest.mark.parametrize(
    "agent", [TrendAgent(), MomentumAgent(), MeanReversionAgent(), RegimeAgent()]
)
def test_no_look_ahead_a_decision_depends_only_on_past_bars(agent: object) -> None:
    closes = trend_up(160)
    full = bars(closes)
    k = 120
    a = agent.analyze(MarketView("EURUSD", "M15", full[:k], NOW, 0.0, 0.0001))  # type: ignore[attr-defined]
    b = agent.analyze(MarketView("EURUSD", "M15", bars(closes[:k]), NOW, 0.0, 0.0001))  # type: ignore[attr-defined]
    assert a.to_dict() == b.to_dict()


def test_scores_are_bounded_and_documented_as_not_probabilities() -> None:
    for agent in (TrendAgent(), MomentumAgent(), MeanReversionAgent(), RegimeAgent()):
        p = agent.analyze(view(trend_up()))
        assert 0.0 <= p.score <= 1.0
    with pytest.raises(ValueError):
        AgentProposal(
            "x",
            AgentRole.TREND,
            "1",
            "EURUSD",
            "M15",
            NOW,
            ProposalAction.OPEN,
            Direction.LONG,
            1.5,
            1.08,
            1.07,
            1.09,
            1.0,
            "r",
        )
    with pytest.raises(ValueError, match="stop loss"):
        AgentProposal(
            "x",
            AgentRole.TREND,
            "1",
            "EURUSD",
            "M15",
            NOW,
            ProposalAction.OPEN,
            Direction.LONG,
            0.5,
            1.08,
            None,
            1.09,
            None,
            "r",
        )


# --- coordinator ------------------------------------------------------------------------


def prop(
    role: AgentRole, action: ProposalAction, d: Direction, score: float, regime: str | None = None
) -> AgentProposal:
    stop = 1.07 if d is Direction.LONG else 1.09 if d is Direction.SHORT else None
    ev = {"regime": regime} if regime else {}
    return AgentProposal(
        f"{role.value}-1",
        role,
        "1.0.0",
        "EURUSD",
        "M15",
        NOW,
        action,
        d,
        score,
        1.08,
        stop,
        None,
        None,
        "t",
        ev,
    )


def regime(name: str, veto: bool = False) -> AgentProposal:
    action = ProposalAction.VETO if veto else ProposalAction.HOLD
    return prop(AgentRole.REGIME, action, Direction.FLAT, 0.5, name)


OPEN, HOLD, CLOSE = ProposalAction.OPEN, ProposalAction.HOLD, ProposalAction.CLOSE
L, S, F = Direction.LONG, Direction.SHORT, Direction.FLAT


def test_coordinator_consensus_in_a_trend() -> None:
    c = PortfolioCoordinator(entry_threshold=0.55)
    props = [
        prop(AgentRole.TREND, OPEN, L, 0.8),
        prop(AgentRole.MOMENTUM, OPEN, L, 0.7),
        prop(AgentRole.MEAN_REVERSION, HOLD, F, 0.0),
    ]
    d = c.decide_pair("EURUSD", regime("trending"), props, 0.0, None)
    assert d.action is OPEN and d.direction is L and d.lead_agent is AgentRole.TREND
    assert d.request is not None and d.request.stop_loss == 1.07 and d.score >= 0.55


def test_coordinator_veto_conflict_and_weak_consensus() -> None:
    c = PortfolioCoordinator(entry_threshold=0.55)
    strong = [
        prop(AgentRole.TREND, OPEN, L, 0.9),
        prop(AgentRole.MOMENTUM, OPEN, L, 0.9),
        prop(AgentRole.MEAN_REVERSION, HOLD, F, 0.0),
    ]
    assert (
        c.decide_pair("EURUSD", regime("volatile", veto=True), strong, 0, None).action
        is ProposalAction.VETO
    )
    conflict = [
        prop(AgentRole.TREND, OPEN, L, 0.9),
        prop(AgentRole.MOMENTUM, HOLD, F, 0),
        prop(AgentRole.MEAN_REVERSION, OPEN, S, 0.9),
    ]
    d = c.decide_pair("EURUSD", regime("mixed"), conflict, 0, None)
    assert d.action is HOLD and "conflito" in d.summary
    weak = [
        prop(AgentRole.TREND, HOLD, F, 0),
        prop(AgentRole.MOMENTUM, HOLD, F, 0),
        prop(AgentRole.MEAN_REVERSION, OPEN, L, 0.9),
    ]
    # mean reversion alone in a trending market weighs little
    assert c.decide_pair("EURUSD", regime("trending"), weak, 0, None).action is HOLD
    # ... but leads in a ranging market
    assert c.decide_pair("EURUSD", regime("ranging"), weak, 0, None).action is OPEN


def test_coordinator_exits_follow_the_owner_and_veto_does_not_block_them() -> None:
    c = PortfolioCoordinator()
    props = [
        prop(AgentRole.TREND, HOLD, F, 0),
        prop(AgentRole.MOMENTUM, CLOSE, F, 0.6),
        prop(AgentRole.MEAN_REVERSION, CLOSE, F, 0.6),
    ]
    keep = c.decide_pair("EURUSD", regime("mixed"), props, 10_000, AgentRole.TREND)
    assert keep.action is HOLD  # other agents' CLOSE does not override the owner
    d = c.decide_pair(
        "EURUSD", regime("volatile", veto=True), props, 10_000, AgentRole.MEAN_REVERSION
    )
    assert d.action is CLOSE and d.request is not None and d.request.direction is F


def test_rank_caps_entries_and_penalises_crowded_currencies() -> None:
    c = PortfolioCoordinator(entry_threshold=0.5, max_new_entries_per_cycle=1)
    props = [
        prop(AgentRole.TREND, OPEN, L, 0.9),
        prop(AgentRole.MOMENTUM, OPEN, L, 0.9),
        prop(AgentRole.MEAN_REVERSION, HOLD, F, 0),
    ]
    eur = c.decide_pair("EURUSD", regime("trending"), props, 0, None)
    gbp = c.decide_pair("GBPUSD", regime("trending"), props, 0, None)
    chosen = c.rank([eur, gbp], exposure={"EUR": 20_000.0, "JPY": -20_000.0})  # a EURJPY long
    assert [d.pair for d in chosen] == ["GBPUSD"]  # EUR long already crowded -> penalised
