"""Dashboard constants: paths, labels and the fixed disclaimers shown in the UI."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data"  # git-ignored
PAPER_ROOT = DATA_ROOT / "paper" / "accounts"
RESEARCH_ROOT = DATA_ROOT / "dashboard" / "research"  # persisted Phase 9/10 results

SYMBOL = "SPY"
PROVIDER = "yfinance"
TIMEFRAME = "1d"
SYSTEM_LABEL = "Daily research system"
PAPER_LABEL = "PAPER SIMULATION"

DISCLAIMERS = {
    "system": (
        "Daily research system based on locally ingested end-of-day data. Not real-time and "
        "not a trading terminal. Nothing here is investment advice."
    ),
    "candles": "Candlestick patterns are descriptive observations, not trading signals.",
    "price_action": (
        "Market-structure labels and events are descriptive observations, not trading signals."
    ),
    "regimes": (
        "Market regimes describe observed conditions. They are not predictions or trade "
        "recommendations."
    ),
    "strategies": (
        "Research strategy states are outputs of fixed baseline rules used as research "
        "benchmarks. They are not recommendations."
    ),
    "backtest": "Historical backtest results do not imply future performance.",
    "validation": (
        "Statistical significance is not economic significance. Results are exploratory and "
        "not evidence of future profitability."
    ),
    "ml": (
        "Model outputs are research estimates from a fixed protocol, not guaranteed market "
        "probabilities."
    ),
    "risk": (
        "The risk layer only reduces or blocks the exposure a strategy requests. It never "
        "creates a signal of its own."
    ),
    "paper": (
        "Paper trading is a local simulation. It does not transmit orders to any brokerage "
        "or market."
    ),
}
