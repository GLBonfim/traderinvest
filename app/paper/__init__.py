"""Phase 12 paper trading: a LOCAL SIMULATION of order execution. It does not transmit orders
to any brokerage or market.

Strategy state -> requested target -> RiskManager -> approved Instruction -> PaperBroker ->
simulated fill at the next session open -> account -> append-only ledger. Two drivers share one
state machine (`PaperTrader`): historical replay (`PaperTradingEngine`) and incremental,
restart-safe processing of completed bars (`PaperStore`). No broker integration, no network,
no credentials, no real-money execution.
"""

from app.paper.broker import PaperBroker
from app.paper.config import MODE, PAPER_VERSION, PaperConfig
from app.paper.engine import (
    PaperRun,
    PaperTradingEngine,
    baseline_inputs,
    build_inputs,
    data_fingerprint,
    replay_baselines,
)
from app.paper.store import PaperStore
from app.paper.trader import BarInput, PaperTrader

__all__ = [
    "MODE",
    "PAPER_VERSION",
    "BarInput",
    "PaperBroker",
    "PaperConfig",
    "PaperRun",
    "PaperStore",
    "PaperTrader",
    "PaperTradingEngine",
    "baseline_inputs",
    "build_inputs",
    "data_fingerprint",
    "replay_baselines",
]
