"""Six fixed baseline rules. Each reads only its declared features, row by row.

Baseline strategies are research benchmarks, not evidence of profitability.
"""

from typing import ClassVar

import numpy as np
import pandas as pd

from app.strategies.base import FLAT, INSUFFICIENT_DATA, LONG, Strategy
from app.strategies.config import StrategyConfig


def _states(index: pd.Index, missing: pd.Series, long: pd.Series) -> pd.Series:
    return pd.Series(
        np.select([missing.to_numpy(bool), long.to_numpy(bool)], [INSUFFICIENT_DATA, LONG], FLAT),
        index=index,
        dtype=object,
    )


class BuyAndHold(Strategy):
    """Benchmark, not a timing rule: LONG on every bar with a valid close."""

    strategy_id: ClassVar[str] = "buy_and_hold"
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = "LONG whenever a close exists (benchmark)"

    @property
    def features(self) -> tuple[str, ...]:
        return ("close",)

    def decide(self, features: pd.DataFrame) -> pd.Series:
        close = features["close"]
        return _states(features.index, close.isna(), close.notna())


class SmaTrend(Strategy):
    strategy_id: ClassVar[str] = "sma_trend"
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = "LONG if close > SMA(n) else FLAT"

    def __init__(self, cfg: StrategyConfig) -> None:
        self.sma = f"sma_{cfg.sma_trend_period}"

    @property
    def features(self) -> tuple[str, ...]:
        return ("close", self.sma)

    def decide(self, features: pd.DataFrame) -> pd.Series:
        close, sma = features["close"], features[self.sma]
        return _states(features.index, close.isna() | sma.isna(), close > sma)


class SmaCrossover(Strategy):
    strategy_id: ClassVar[str] = "sma_crossover"
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = "LONG if SMA(fast) > SMA(slow) else FLAT"

    def __init__(self, cfg: StrategyConfig) -> None:
        self.fast = f"sma_{cfg.crossover_fast_period}"
        self.slow = f"sma_{cfg.crossover_slow_period}"

    @property
    def features(self) -> tuple[str, ...]:
        return (self.fast, self.slow)

    def decide(self, features: pd.DataFrame) -> pd.Series:
        fast, slow = features[self.fast], features[self.slow]
        return _states(features.index, fast.isna() | slow.isna(), fast > slow)


class RsiMomentum(Strategy):
    strategy_id: ClassVar[str] = "rsi_momentum"
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = "LONG if RSI(n) > midline else FLAT"

    def __init__(self, cfg: StrategyConfig) -> None:
        self.rsi = f"rsi_{cfg.rsi_period}"
        self.midline = cfg.rsi_midline

    @property
    def features(self) -> tuple[str, ...]:
        return (self.rsi,)

    def decide(self, features: pd.DataFrame) -> pd.Series:
        rsi = features[self.rsi]
        return _states(features.index, rsi.isna(), rsi > self.midline)


class _LabelRule(Strategy):
    """LONG when a categorical label equals `long_label`; INSUFFICIENT_DATA when the label is
    missing or 'insufficient_data'; FLAT for every other known label."""

    column: ClassVar[str]
    long_label: ClassVar[str]

    @property
    def features(self) -> tuple[str, ...]:
        return (self.column,)

    def decide(self, features: pd.DataFrame) -> pd.Series:
        label = features[self.column]
        missing = label.isna() | (label == "insufficient_data")
        return _states(features.index, missing, label == self.long_label)


class PriceActionTrend(_LabelRule):
    strategy_id: ClassVar[str] = "price_action_trend"
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = "LONG if price-action structure == uptrend else FLAT"
    column: ClassVar[str] = "pa_structure"
    long_label: ClassVar[str] = "uptrend"


class RegimeTrend(_LabelRule):
    strategy_id: ClassVar[str] = "regime_trend"
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = "LONG if composite regime == trending_up else FLAT"
    column: ClassVar[str] = "regime_composite"
    long_label: ClassVar[str] = "trending_up"


def baseline_strategies(cfg: StrategyConfig) -> tuple[Strategy, ...]:
    return (
        BuyAndHold(),
        SmaTrend(cfg),
        SmaCrossover(cfg),
        RsiMomentum(cfg),
        PriceActionTrend(),
        RegimeTrend(),
    )
