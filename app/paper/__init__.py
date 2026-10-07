"""Phase 12 paper trading: internal deterministic broker, HISTORICAL REPLAY only.

Strategy state -> requested target -> RiskManager -> approved target -> PaperBroker ->
simulated fill -> portfolio -> audit log. No broker integration, no network, no credentials,
no real-money execution.
"""

from app.paper.broker import PaperBroker
from app.paper.config import MODE, PAPER_VERSION, PaperConfig
from app.paper.engine import PaperRun, PaperTradingEngine, data_fingerprint, replay_baselines

__all__ = [
    "MODE",
    "PAPER_VERSION",
    "PaperBroker",
    "PaperConfig",
    "PaperRun",
    "PaperTradingEngine",
    "data_fingerprint",
    "replay_baselines",
]
