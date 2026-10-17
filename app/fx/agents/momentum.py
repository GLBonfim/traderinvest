"""Momentum agent: RSI(14), ROC(10) and a rising MACD histogram must agree.

LONG when 55 <= RSI <= 75, ROC > 0 and the MACD histogram is positive and rising; SHORT
symmetrically (25 <= RSI <= 45). RSI beyond 75/25 is treated as exhaustion (no entry).
Stop 1.5 ATR, target 2.5 ATR.
"""

from app.fx.agents.base import MarketView, clip01, hold, make
from app.fx.models import Direction
from app.fx.proposals import AgentProposal, AgentRole, ProposalAction
from app.indicators.indicators import macd, roc, rsi


class MomentumAgent:
    role = AgentRole.MOMENTUM
    version = "1.0.0"
    min_bars = 60

    def analyze(self, view: MarketView) -> AgentProposal:
        if len(view.bars) < self.min_bars:
            return hold(self, view, "dados insuficientes", {"bars": len(view.bars)})
        df = view.df
        close = df["close"]
        rv = float(rsi(close, 14).iloc[-1])
        rc = float(roc(close, 10).iloc[-1])
        m = macd(close, 12, 26, 9)["macd_hist"]
        h, h_prev = float(m.iloc[-1]), float(m.iloc[-2])
        av = float(view.atr14.iloc[-1])
        c = float(close.iloc[-1])
        ev = {"rsi": rv, "roc": rc, "macd_hist": h, "macd_hist_prev": h_prev, "atr": av}
        up = 55 <= rv <= 75 and rc > 0 and h > 0 and h > h_prev
        down = 25 <= rv <= 45 and rc < 0 and h < 0 and h < h_prev
        if up or down:
            sign = 1 if up else -1
            strength = abs(rv - 50) / 25  # 0.2 .. 1.0 inside the band
            accel = min(abs(h - h_prev) / (abs(h) + 1e-12), 1.0)
            score = clip01(0.4 + 0.35 * strength + 0.25 * accel)
            if view.position_units * sign < 0:
                return make(
                    self,
                    view,
                    ProposalAction.CLOSE,
                    Direction.FLAT,
                    score,
                    "momentum contra a posição",
                    ev,
                )
            d = Direction.LONG if up else Direction.SHORT
            return make(
                self,
                view,
                ProposalAction.OPEN,
                d,
                score,
                "momentum " + ("comprador" if up else "vendedor") + " a acelerar",
                ev,
                c,
                c - sign * 1.5 * av,
                c + sign * 2.5 * av,
            )
        why = "exaustão (RSI extremo)" if rv > 75 or rv < 25 else "momentum fraco ou misto"
        return hold(self, view, why, ev)
