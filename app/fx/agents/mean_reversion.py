"""Mean Reversion agent v2: fade a CONFIRMED rejection in a ranging market.

Only when the efficiency ratio shows a ranging market: the previous bar closed outside the
Bollinger band with an extreme RSI and the current bar closed back inside; target the band's
middle, stop beyond the recent swing plus an ATR buffer, minimum risk/reward 1.2, time stop.
Logic: app.fx.agents.signals.mean_reversion_signals (validated code).
"""

from app.fx.agents import calibrated
from app.fx.agents.base import MarketView, from_signal, hold, signal_row
from app.fx.agents.signals import MeanReversionParams, mean_reversion_signals
from app.fx.proposals import AgentProposal, AgentRole


class MeanReversionAgent:
    role = AgentRole.MEAN_REVERSION
    version = "2.0.0"
    min_bars = 260

    def __init__(self, params: MeanReversionParams | None = None) -> None:
        self.params = params or calibrated.MEAN_REVERSION

    def analyze(self, view: MarketView) -> AgentProposal:
        if len(view.bars) < self.min_bars:
            return hold(self, view, "dados insuficientes", {"bars": len(view.bars)})
        sig, feat = signal_row(view, mean_reversion_signals, self.params)
        return from_signal(
            self,
            view,
            sig,
            feat,
            "rejeição confirmada abaixo da banda em mercado lateral",
            "rejeição confirmada acima da banda em mercado lateral",
            self.params.max_hold,
        )
