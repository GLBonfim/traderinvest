"""Research backtester: hypothetical trades, costs and metrics from strategy states.

No orders are ever sent. Results describe a historical simulation under explicit assumptions
and are not evidence of future profitability.
"""

from app.backtest.config import ENGINE_VERSION, BacktestConfig
from app.backtest.engine import (
    BENCHMARK_PRICE,
    BENCHMARK_TOTAL,
    Backtester,
    run_suite,
    summary_table,
)
from app.backtest.models import BacktestResult

__all__ = [
    "BENCHMARK_PRICE",
    "BENCHMARK_TOTAL",
    "ENGINE_VERSION",
    "BacktestConfig",
    "BacktestResult",
    "Backtester",
    "run_suite",
    "summary_table",
]
