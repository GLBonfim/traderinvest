"""Calibrated parameters and approval status of the v2 agents.

Source: walk-forward study on IBKR M15 bid/ask history, 9 pairs, 2020-10-25..2026-10-16
(`python -m app.fx.research.study`, report docs/fx-agent-validation.md). Parameters below are
the development-period choices (robust t-statistic, grid-neighbour smoothing) on the system
timeframe M15 (H1 scored worse on development data).

Result of the pre-registered holdout (2025-10-01..2026-10-17, evaluated once, pre-registration
sha256 c2e853491dc8cbf3...): NO agent passed the rule. Trend and Momentum lost about -0.25 to
-0.29 R per trade net of costs over ~3 200 trades each (no better than random timing); Mean
Reversion was slightly positive (+0.09 R) on only 69 trades, statistically insignificant and
negative on validation. Therefore APPROVED is empty: the agents keep analysing and their
proposals are shown as "em observação", but the coordinator never lets them open a position.
Only `simulation.allow_unapproved_agents` (SIMULATION, synthetic prices) overrides this, for
demonstrations of the interface.
"""

from app.fx.agents.signals import (
    DEFAULT_REGIME,
    MeanReversionParams,
    MomentumParams,
    RegimeParams,
    TrendParams,
)

TIMEFRAME = "M15"
TREND = TrendParams(stop_atr=2.5, target_atr=4.0, min_slope_atr=0.6)
MOMENTUM = MomentumParams(channel=40, stop_atr=2.0, target_atr=3.0)
MEAN_REVERSION = MeanReversionParams(band_k=2.5, max_er=0.2, stop_buffer_atr=1.0)
REGIME: RegimeParams = DEFAULT_REGIME

# roles allowed to open positions (pre-registered holdout rule passed): none
APPROVED: frozenset[str] = frozenset()
PROVENANCE = (
    "IBKR M15 2020-10..2026-10, holdout 2025-10..2026-10 avaliado uma vez; "
    "nenhum agente aprovado (pré-registo c2e85349)"
)
