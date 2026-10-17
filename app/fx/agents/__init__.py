"""The five agents: Trend Following, Momentum, Mean Reversion, Volatility/Regime filter and the
Portfolio Coordinator. Agents propose; only the risk manager authorizes (ADR-0036)."""

from app.fx.agents.coordinator import PortfolioCoordinator
from app.fx.agents.mean_reversion import MeanReversionAgent
from app.fx.agents.momentum import MomentumAgent
from app.fx.agents.regime import RegimeAgent
from app.fx.agents.trend import TrendAgent

__all__ = [
    "MeanReversionAgent",
    "MomentumAgent",
    "PortfolioCoordinator",
    "RegimeAgent",
    "TrendAgent",
]
