"""Trend Following agent v2: join an established trend on a pullback.

LONG when the close is above the higher-timeframe EMA(200), EMA(20) > EMA(50) with the slow EMA
rising by at least `min_slope_atr` ATR over `slope_bars`, and the price pulled back to EMA(20)
within the last bars then closed back above it; SHORT symmetrically. ATR stop and target, time
stop after `max_hold` bars. Logic: app.fx.agents.signals.trend_signals (validated code).
"""

from app.fx.agents import calibrated
from app.fx.agents.base import MarketView, from_signal, hold, signal_row
from app.fx.agents.signals import TrendParams, trend_signals
from app.fx.proposals import AgentProposal, AgentRole


class TrendAgent:
    role = AgentRole.TREND
    version = "2.0.0"
    min_bars = 260

    def __init__(self, params: TrendParams | None = None) -> None:
        self.params = params or calibrated.TREND

    def analyze(self, view: MarketView) -> AgentProposal:
        if len(view.bars) < self.min_bars:
            return hold(self, view, "dados insuficientes", {"bars": len(view.bars)})
        sig, feat = signal_row(view, trend_signals, self.params)
        return from_signal(
            self,
            view,
            sig,
            feat,
            "tendência de alta: recuo à EMA20 com contexto H1 favorável",
            "tendência de baixa: recuo à EMA20 com contexto H1 favorável",
            self.params.max_hold,
        )
