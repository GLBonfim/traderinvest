"""Calibrated parameters and approval status of the v2 agents.

Filled from the walk-forward study (`python -m app.fx.research.study`, report in
docs/fx-agent-validation.md). Until an agent passes the pre-registered holdout rule it is NOT
approved: it keeps analysing and its proposals are shown, but the coordinator never lets it open
a position.
"""

from app.fx.agents.signals import (
    DEFAULT_MEAN_REVERSION,
    DEFAULT_MOMENTUM,
    DEFAULT_REGIME,
    DEFAULT_TREND,
    MeanReversionParams,
    MomentumParams,
    RegimeParams,
    TrendParams,
)

TREND: TrendParams = DEFAULT_TREND
MOMENTUM: MomentumParams = DEFAULT_MOMENTUM
MEAN_REVERSION: MeanReversionParams = DEFAULT_MEAN_REVERSION
REGIME: RegimeParams = DEFAULT_REGIME

# roles allowed to open positions (pre-registered holdout rule passed)
APPROVED: frozenset[str] = frozenset()
PROVENANCE = "not calibrated yet"
