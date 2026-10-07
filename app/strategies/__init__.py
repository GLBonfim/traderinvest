"""Baseline strategies: fixed, transparent rules producing hypothetical LONG/FLAT states.

Baseline strategies are research benchmarks, not evidence of profitability. Nothing here
creates orders, prices, sizes or executions.
"""

from app.strategies.base import FLAT, INSUFFICIENT_DATA, LONG, Strategy
from app.strategies.config import ENGINE_VERSION, StrategyConfig
from app.strategies.engine import StrategyEngine, StrategyRun

__all__ = [
    "ENGINE_VERSION",
    "FLAT",
    "INSUFFICIENT_DATA",
    "LONG",
    "Strategy",
    "StrategyConfig",
    "StrategyEngine",
    "StrategyRun",
]
