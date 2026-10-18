"""Volatility / Regime filter agent v2: classifies each pair and vetoes new entries when
volatility is extreme or dead (ATR percentile), when the spread is too large relative to the
ATR, or outside liquid hours / around the rollover. Logic: app.fx.agents.signals.regime_frame
(validated code). It proposes no direction; `evidence["regime"]` is shown to the coordinator.
"""

from typing import Any

import pandas as pd

from app.fx.agents import calibrated
from app.fx.agents.base import MarketView, hold, make
from app.fx.agents.signals import RegimeParams, features, regime_frame
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
    version = "2.0.0"
    min_bars = 120

    def __init__(self, params: RegimeParams | None = None) -> None:
        self.params = params or calibrated.REGIME

    def analyze(self, view: MarketView) -> AgentProposal:
        if len(view.bars) < self.min_bars:
            return hold(
                self, view, "dados insuficientes", {"bars": len(view.bars), "regime": MIXED}
            )
        spread = pd.Series(view.spread, index=view.df.index, dtype=float)
        f = features(view.df, spread)
        last = regime_frame(f, self.params).iloc[-1]
        lf = f.iloc[-1]
        regime = str(last["regime"])
        ev: dict[str, Any] = {
            "regime": regime,
            "efficiency_ratio": float(lf["er"]),
            "atr_percentile": float(lf["atr_pct"]),
            "spread_to_atr": float(view.spread / lf["atr"]) if lf["atr"] > 0 else None,
            "tradable_hours": bool(lf["tradable"]),
        }
        if bool(last["veto"]):
            if not bool(lf["tradable"]):
                why = "fora das horas líquidas ou junto ao rollover"
            elif regime == VOLATILE:
                why = "volatilidade extrema"
            elif regime == QUIET:
                why = "mercado parado: o spread não compensa"
            else:
                why = "spread demasiado largo face à volatilidade"
            return make(
                self, view, ProposalAction.VETO, Direction.FLAT, 1.0, f"entradas vetadas: {why}", ev
            )
        return make(
            self,
            view,
            ProposalAction.HOLD,
            Direction.FLAT,
            0.5,
            f"regime: {REGIME_LABELS.get(regime, regime).lower()}",
            ev,
        )
