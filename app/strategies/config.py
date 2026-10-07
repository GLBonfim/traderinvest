"""Fixed, pre-declared parameters of the baseline strategies.

These are textbook conventions (SMA 200, SMA 20/50, RSI 14 vs 50). They are NOT optimised and
must not be tuned on returns, Sharpe or any other outcome. Definitions:
docs/baseline-strategies.md.
"""

import hashlib
import json
from dataclasses import asdict, dataclass

# Bump when any rule or timestamp convention changes.
ENGINE_VERSION = "1.0.0"


@dataclass(frozen=True)
class StrategyConfig:
    sma_trend_period: int = 200
    crossover_fast_period: int = 20
    crossover_slow_period: int = 50
    rsi_period: int = 14
    rsi_midline: float = 50.0
    calendar: str = "XNYS"  # exchange calendar used for observed_at / effective_at

    def __post_init__(self) -> None:
        for name in (
            "sma_trend_period",
            "crossover_fast_period",
            "crossover_slow_period",
            "rsi_period",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.crossover_fast_period >= self.crossover_slow_period:
            raise ValueError("crossover_fast_period must be < crossover_slow_period")
        if not 0 < self.rsi_midline < 100:
            raise ValueError("rsi_midline must be in (0, 100)")

    def fingerprint(self) -> str:
        payload = json.dumps({"engine": ENGINE_VERSION, **asdict(self)}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
