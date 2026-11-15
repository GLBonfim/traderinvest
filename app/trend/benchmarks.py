"""Benchmarks (design v2 section 9): equal weight, global 60/40 and EUR cash.

Both invested benchmarks start in cash, buy at D of the run's first day and then rebalance
every asset to its fixed weight at D of the period end (equal weight: month; 60/40: year),
through the same simulator, fills, units and costs as the strategy.
"""

from dataclasses import dataclass

import pandas as pd

from app.trend.data import is_period_end
from app.trend.simulator import Context


@dataclass
class FixedWeightPolicy:
    weights: dict[str, float]
    freq: str  # "M" or "Y"
    scheduled: pd.DatetimeIndex

    @property
    def assets(self) -> tuple[str, ...]:
        return tuple(sorted(self.weights))

    def decide(self, ctx: Context) -> dict[str, float]:
        if ctx.first or is_period_end(ctx.day, self.scheduled, self.freq, ctx.prev_day):
            return {a: w for a, w in self.weights.items() if a not in ctx.pending}
        return {}


def equal_weight(asset_ids: tuple[str, ...], scheduled: pd.DatetimeIndex) -> FixedWeightPolicy:
    return FixedWeightPolicy({a: 1.0 / len(asset_ids) for a in asset_ids}, "M", scheduled)


def sixty_forty(legs: dict[str, float], scheduled: pd.DatetimeIndex) -> FixedWeightPolicy:
    if abs(sum(legs.values()) - 1.0) > 1e-12:
        raise ValueError("60/40 weights must sum to 1")
    return FixedWeightPolicy(dict(legs), "Y", scheduled)


@dataclass
class CashPolicy:
    assets: tuple[str, ...] = ()

    def decide(self, ctx: Context) -> dict[str, float]:
        return {}
