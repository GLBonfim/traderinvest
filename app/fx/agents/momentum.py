"""Momentum agent v2: breakout of the previous range with expanding volatility.

LONG when the close breaks the highest high of the previous `channel` bars, above the
higher-timeframe EMA(200), with ATR above its 50-bar average and RSI in [rsi_min, rsi_max]
(strong, not exhausted); SHORT symmetrically. ATR stop and target, time stop. Logic:
app.fx.agents.signals.momentum_signals (validated code).
"""

from app.fx.agents import calibrated
from app.fx.agents.base import MarketView, from_signal, hold, signal_row
from app.fx.agents.signals import MomentumParams, momentum_signals
from app.fx.proposals import AgentProposal, AgentRole


class MomentumAgent:
    role = AgentRole.MOMENTUM
    version = "2.0.0"
    min_bars = 260

    def __init__(self, params: MomentumParams | None = None) -> None:
        self.params = params or calibrated.MOMENTUM

    def analyze(self, view: MarketView) -> AgentProposal:
        if len(view.bars) < self.min_bars:
            return hold(self, view, "dados insuficientes", {"bars": len(view.bars)})
        sig, feat = signal_row(view, momentum_signals, self.params)
        return from_signal(
            self,
            view,
            sig,
            feat,
            "rompimento de alta com expansão de volatilidade",
            "rompimento de baixa com expansão de volatilidade",
            self.params.max_hold,
        )
