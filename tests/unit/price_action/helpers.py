"""Synthetic OHLCV builders for Price Action Engine tests. NOT market data."""

from collections.abc import Sequence
from itertools import pairwise

import numpy as np
import pandas as pd

from app.price_action.config import PriceActionConfig
from tests.unit.candles.helpers import Candle, bars

# Short pivots keep scenarios small: a pivot needs 2 bars on each side.
FAST = PriceActionConfig(swing_left_bars=2, swing_right_bars=2)


def hl_bars(highs: Sequence[float], lows: Sequence[float] | None = None) -> pd.DataFrame:
    """Bars from highs (and lows); open = close = midpoint."""
    lows = list(lows) if lows is not None else [h - 1 for h in highs]
    return bars([((h + lo) / 2, h, lo, (h + lo) / 2) for h, lo in zip(highs, lows, strict=True)])


def zigzag(points: Sequence[float], step: int = 4, wick: float = 0.1) -> pd.DataFrame:
    """Closes interpolated linearly between turning points; peaks/troughs become pivots."""
    closes: list[float] = [points[0]]
    for a, b in pairwise(points):
        closes.extend(np.linspace(a, b, step + 1)[1:].tolist())
    candles: list[Candle] = []
    prev = closes[0]
    for close in closes:
        candles.append((prev, max(prev, close) + wick, min(prev, close) - wick, close))
        prev = close
    return bars(candles)


def mirror(candles: Sequence[Candle], axis: float = 200.0) -> list[Candle]:
    """Reflect prices around `axis`: highs become lows, breakouts become breakdowns."""
    return [(axis - o, axis - lo, axis - h, axis - c) for o, h, lo, c in candles]


# A swing high at 110 (bar 4) confirmed at bar 6 (k=2); price then sits below it.
BASE: list[Candle] = [
    (100, 101, 99, 100),
    (100, 102, 99.5, 101),
    (101, 103, 100, 102),
    (102, 104, 101, 103),
    (103, 110, 102, 104),  # pivot high 110
    (104, 105, 101, 102),
    (102, 103, 100.5, 101),  # confirms the pivot -> zone [110, 110] available from here
    (101, 104, 100.6, 103),
]
