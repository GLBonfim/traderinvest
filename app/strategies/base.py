"""Strategy contract: an explicit, fixed rule from declared features to a state per bar.

Baseline strategies are research benchmarks, not evidence of profitability. A state is a
hypothetical stance decided at the close of bar T; it is not an order, has no price and no size.
"""

from abc import ABC, abstractmethod
from typing import ClassVar

import pandas as pd

LONG = "LONG"
FLAT = "FLAT"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
STATES = (LONG, FLAT, INSUFFICIENT_DATA)


class Strategy(ABC):
    strategy_id: ClassVar[str]
    version: ClassVar[str]
    description: ClassVar[str]

    @property
    @abstractmethod
    def features(self) -> tuple[str, ...]:
        """Feature columns this rule reads. The engine passes ONLY these columns to `decide`."""

    @abstractmethod
    def decide(self, features: pd.DataFrame) -> pd.Series:
        """State per bar (LONG / FLAT / INSUFFICIENT_DATA) from features available at that bar.

        Must be row-wise or causal: the state at T may only depend on feature rows <= T.
        Missing inputs must yield INSUFFICIENT_DATA, never FLAT.
        """
