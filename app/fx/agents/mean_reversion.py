"""Mean Reversion agent: Bollinger(20, 2) excursions confirmed by RSI(14).

LONG when the close is below the lower band and RSI < 32 (target: the middle band); SHORT when
above the upper band and RSI > 68. Stop 1.5 ATR beyond the entry. An open position that has
reached the middle band gets a CLOSE proposal.
"""

from app.fx.agents.base import MarketView, clip01, hold, make
from app.fx.models import Direction
from app.fx.proposals import AgentProposal, AgentRole, ProposalAction
from app.indicators.indicators import bollinger, rsi


class MeanReversionAgent:
    role = AgentRole.MEAN_REVERSION
    version = "1.0.0"
    min_bars = 40

    def analyze(self, view: MarketView) -> AgentProposal:
        if len(view.bars) < self.min_bars:
            return hold(self, view, "dados insuficientes", {"bars": len(view.bars)})
        df = view.df
        close = df["close"]
        bb = bollinger(close, 20, 2.0, 0).iloc[-1]
        rv = float(rsi(close, 14).iloc[-1])
        av = float(view.atr14.iloc[-1])
        c = float(close.iloc[-1])
        mid, up, lo = float(bb["bb_middle"]), float(bb["bb_upper"]), float(bb["bb_lower"])
        pb = float(bb["bb_percent_b"])
        ev = {
            "close": c,
            "bb_middle": mid,
            "bb_upper": up,
            "bb_lower": lo,
            "percent_b": pb,
            "rsi": rv,
            "atr": av,
        }
        if (view.position_units > 0 and c >= mid) or (view.position_units < 0 and c <= mid):
            return make(
                self, view, ProposalAction.CLOSE, Direction.FLAT, 0.6, "preço regressou à média", ev
            )
        if c < lo and rv < 32:
            score = clip01(0.5 + 0.3 * min(-pb, 0.5) / 0.5 + 0.2 * (32 - rv) / 32)
            return make(
                self,
                view,
                ProposalAction.OPEN,
                Direction.LONG,
                score,
                "excesso vendedor abaixo da banda inferior",
                ev,
                c,
                c - 1.5 * av,
                mid,
            )
        if c > up and rv > 68:
            score = clip01(0.5 + 0.3 * min(pb - 1, 0.5) / 0.5 + 0.2 * (rv - 68) / 32)
            return make(
                self,
                view,
                ProposalAction.OPEN,
                Direction.SHORT,
                score,
                "excesso comprador acima da banda superior",
                ev,
                c,
                c + 1.5 * av,
                mid,
            )
        return hold(self, view, "preço dentro das bandas", ev)
