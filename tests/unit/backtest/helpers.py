"""Small hand-made bar series on real XNYS sessions (synthetic, not market data)."""

from datetime import date

import pandas as pd

from app.backtest.config import BacktestConfig
from tests.unit.strategies.helpers import XNYS

FREE = BacktestConfig(
    initial_capital=100.0, commission_per_trade=0.0, spread_bps=0.0, slippage_bps=0.0
)


def bars_from(
    opens: list[float], closes: list[float], start: date = date(2024, 7, 1)
) -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        XNYS.sessions(start, date(2030, 1, 1))["open_utc"][: len(opens)], name="ts"
    )
    hi = [max(o, c) + 1 for o, c in zip(opens, closes, strict=True)]
    lo = [min(o, c) - 1 for o, c in zip(opens, closes, strict=True)]
    return pd.DataFrame(
        {"open": opens, "high": hi, "low": lo, "close": closes, "volume": 1e6, "adj_close": closes},
        index=idx,
    ).astype("float64")


def states(bars: pd.DataFrame, values: list[str]) -> pd.Series:
    return pd.Series(values, index=bars.index)
