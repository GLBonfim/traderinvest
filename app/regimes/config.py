"""Parameters of the Market Regime Engine.

Defaults are explicit v1.0.0 CONVENTIONS chosen for mathematical clarity (textbook RSI levels,
quantile cut-offs, one-to-three-year reference windows). They were NOT selected by looking at
returns or any trading outcome. Exact definitions: docs/market-regimes.md.
"""

import hashlib
import json
from dataclasses import asdict, dataclass

# Bump when any classification rule changes (parameters versioned via `fingerprint`).
ENGINE_VERSION = "1.0.0"


@dataclass(frozen=True)
class RegimeConfig:
    # ── volatility: causal percentile of realized volatility ──
    volatility_lookback: int = 756  # previous bars in the reference window (~3 trading years)
    volatility_min_observations: int = 252  # ~1 trading year of valid references required
    volatility_low_below: float = 0.20  # percentile <  0.20 -> low
    volatility_high_from: float = 0.80  # percentile >= 0.80 -> high
    volatility_extreme_from: float = 0.95  # percentile >= 0.95 -> extreme
    # ── momentum: RSI / ROC / MACD agreement ──
    momentum_rsi_mid: float = 50.0
    momentum_rsi_extreme_high: float = 70.0
    momentum_rsi_extreme_low: float = 30.0
    # ── instrument-level volume participation (NOT market breadth) ──
    participation_low_below: float = 0.5  # relative volume <  0.5 -> low
    participation_high_from: float = 1.5  # relative volume >= 1.5 -> high

    def __post_init__(self) -> None:
        for name in ("volatility_lookback", "volatility_min_observations"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.volatility_min_observations > self.volatility_lookback:
            raise ValueError("volatility_min_observations must be <= volatility_lookback")
        if (
            not 0
            < self.volatility_low_below
            < self.volatility_high_from
            < self.volatility_extreme_from
            < 1
        ):
            raise ValueError("need 0 < low_below < high_from < extreme_from < 1")
        if (
            not 0
            < self.momentum_rsi_extreme_low
            < self.momentum_rsi_mid
            < self.momentum_rsi_extreme_high
            < 100
        ):
            raise ValueError("need 0 < rsi_extreme_low < rsi_mid < rsi_extreme_high < 100")
        if not 0 < self.participation_low_below < 1 < self.participation_high_from:
            raise ValueError("need 0 < participation_low_below < 1 < participation_high_from")

    def fingerprint(self) -> str:
        payload = json.dumps({"engine": ENGINE_VERSION, **asdict(self)}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
