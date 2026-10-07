"""Candlestick Engine: OHLCV -> candle geometry -> pattern observations.

Patterns are OBSERVATIONS (hypotheses/features), never trading signals. Nothing in this package
produces buy/sell decisions, prices to act on, or claims about future returns.
"""

from app.candles.config import ENGINE_VERSION, CandleConfig
from app.candles.engine import CandleAnalysis, CandlestickEngine, CandlestickObservation

__all__ = [
    "ENGINE_VERSION",
    "CandleAnalysis",
    "CandleConfig",
    "CandlestickEngine",
    "CandlestickObservation",
]
