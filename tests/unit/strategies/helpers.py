"""Synthetic bars on real XNYS session opens (strategy timestamps come from the calendar)."""

from datetime import date

import numpy as np
import pandas as pd

from app.data.calendar import TradingCalendar
from app.indicators.config import IndicatorConfig
from app.indicators.engine import IndicatorEngine
from app.price_action.engine import PriceActionEngine
from app.regimes.config import RegimeConfig
from app.regimes.engine import RegimeEngine
from app.strategies.config import StrategyConfig
from app.strategies.engine import StrategyEngine
from tests.unit.indicators.test_engine import SMALL as IND_SMALL
from tests.unit.price_action.helpers import FAST as PA_FAST

XNYS = TradingCalendar("XNYS")
SMALL = StrategyConfig(
    sma_trend_period=20, crossover_fast_period=5, crossover_slow_period=10, rsi_period=4
)


def session_walk(n: int, seed: int, start: date = date(2015, 1, 2)) -> pd.DataFrame:
    """Seeded random walk indexed by consecutive XNYS session opens (not market data)."""
    opens = pd.DatetimeIndex(XNYS.sessions(start, date(2030, 1, 1))["open_utc"][:n], name="ts")
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    open_ = np.r_[100.0, close[:-1]] * (1 + rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    volume = rng.integers(5e5, 2e6, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=opens
    )


def small_engine(**kw: object) -> StrategyEngine:
    return StrategyEngine(
        SMALL,
        indicator_engine=IndicatorEngine(IndicatorConfig(sma_periods=(5, 10, 20), rsi_period=4)),
        price_action_engine=PriceActionEngine(PA_FAST),
        regime_engine=RegimeEngine(
            RegimeConfig(volatility_lookback=40, volatility_min_observations=20),
            price_action_config=PA_FAST,
            indicator_config=IND_SMALL,
        ),
        **kw,  # type: ignore[arg-type]
    )
