"""Volatility / Regime filter agent: classifies each pair and can veto new entries.

- Kaufman efficiency ratio ER(20) = |net move| / sum(|moves|): >= 0.30 TRENDING, <= 0.18
  RANGING, otherwise MIXED.
- ATR(14) percentile over the last 200 bars: >= 0.95 VOLATILE (veto: moves too violent for the
  stops), <= 0.05 QUIET (veto: too little movement to pay the spread).
- Spread > 25 % of ATR: veto (the cost would eat the edge).

It proposes no direction; its `evidence["regime"]` drives the coordinator's agent weights.
"""

from typing import Any

from app.fx.agents.base import MarketView, hold, make
from app.fx.models import Direction
from app.fx.proposals import AgentProposal, AgentRole, ProposalAction

TRENDING, RANGING, MIXED, VOLATILE, QUIET = "trending", "ranging", "mixed", "volatile", "quiet"
REGIME_LABELS = {
    TRENDING: "Tendência",
    RANGING: "Lateral",
    MIXED: "Misto",
    VOLATILE: "Volátil (bloqueio)",
    QUIET: "Parado (bloqueio)",
}


class RegimeAgent:
    role = AgentRole.REGIME
    version = "1.0.0"
    min_bars = 60

    def analyze(self, view: MarketView) -> AgentProposal:
        if len(view.bars) < self.min_bars:
            return hold(
                self, view, "dados insuficientes", {"bars": len(view.bars), "regime": MIXED}
            )
        df = view.df
        close = df["close"]
        n = 20
        net = abs(float(close.iloc[-1] - close.iloc[-1 - n]))
        path = float(close.diff().abs().iloc[-n:].sum())
        er = net / path if path > 0 else 0.0
        a = view.atr14.dropna()
        av = float(a.iloc[-1])
        window = a.iloc[-200:]
        pct = float((window <= av).mean())
        spread_ratio = view.spread / av if av > 0 else 1.0
        ev: dict[str, Any] = {
            "efficiency_ratio": er,
            "atr": av,
            "atr_percentile": pct,
            "spread_to_atr": spread_ratio,
        }
        if pct >= 0.95 and len(window) >= 100:
            ev["regime"] = VOLATILE
            return make(
                self,
                view,
                ProposalAction.VETO,
                Direction.FLAT,
                pct,
                "volatilidade extrema: entradas vetadas",
                ev,
            )
        if pct <= 0.05 and len(window) >= 100:
            ev["regime"] = QUIET
            return make(
                self,
                view,
                ProposalAction.VETO,
                Direction.FLAT,
                1 - pct,
                "mercado parado: o spread não compensa",
                ev,
            )
        if spread_ratio > 0.25:
            ev["regime"] = MIXED
            return make(
                self,
                view,
                ProposalAction.VETO,
                Direction.FLAT,
                min(spread_ratio, 1.0),
                "spread demasiado largo face à volatilidade",
                ev,
            )
        regime = TRENDING if er >= 0.30 else RANGING if er <= 0.18 else MIXED
        ev["regime"] = regime
        return make(
            self,
            view,
            ProposalAction.HOLD,
            Direction.FLAT,
            er,
            f"regime: {REGIME_LABELS[regime].lower()}",
            ev,
        )
