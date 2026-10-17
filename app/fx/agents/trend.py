"""Trend Following agent: EMA(20)/EMA(50) alignment with a rising/falling slow EMA.

LONG when close > EMA20 > EMA50 and EMA50 rose by at least 0.5 ATR over the last 20 bars;
SHORT symmetrically. The score
combines the EMA separation (in ATRs) and the slope strength. Stop 2 ATR, target 3 ATR.
An existing position against the trend gets a CLOSE proposal.
"""

from app.fx.agents.base import MarketView, clip01, hold, make
from app.fx.models import Direction
from app.fx.proposals import AgentProposal, AgentRole, ProposalAction
from app.indicators.indicators import ema


class TrendAgent:
    role = AgentRole.TREND
    version = "1.0.0"
    min_bars = 70

    def __init__(
        self, fast: int = 20, slow: int = 50, slope_bars: int = 20, min_slope_atr: float = 0.5
    ) -> None:
        self.fast, self.slow, self.slope_bars = fast, slow, slope_bars
        self.min_slope_atr = min_slope_atr

    def analyze(self, view: MarketView) -> AgentProposal:
        if len(view.bars) < self.min_bars:
            return hold(self, view, "dados insuficientes", {"bars": len(view.bars)})
        df = view.df
        close = df["close"]
        f, s, a = ema(close, self.fast), ema(close, self.slow), view.atr14
        c, fv, sv, av = (
            float(close.iloc[-1]),
            float(f.iloc[-1]),
            float(s.iloc[-1]),
            float(a.iloc[-1]),
        )
        slope = (sv - float(s.iloc[-1 - self.slope_bars])) / av if av > 0 else 0.0
        ev = {"close": c, "ema_fast": fv, "ema_slow": sv, "atr": av, "slope_atr": slope}
        sep = (fv - sv) / av if av > 0 else 0.0
        up = c > fv > sv and slope >= self.min_slope_atr
        down = c < fv < sv and slope <= -self.min_slope_atr
        if up or down:
            d = Direction.LONG if up else Direction.SHORT
            score = clip01(
                0.45 + 0.25 * min(abs(sep), 2.0) / 2.0 + 0.3 * min(abs(slope) / 3.0, 1.0)
            )
            sign = 1 if up else -1
            if view.position_units * sign < 0:
                return make(
                    self,
                    view,
                    ProposalAction.CLOSE,
                    Direction.FLAT,
                    score,
                    "tendência inverteu contra a posição",
                    ev,
                )
            label = "alta" if up else "baixa"
            return make(
                self,
                view,
                ProposalAction.OPEN,
                d,
                score,
                f"tendência de {label}: preço e EMAs alinhados",
                ev,
                c,
                c - sign * 2.0 * av,
                c + sign * 3.0 * av,
            )
        return hold(self, view, "sem tendência clara", ev)
