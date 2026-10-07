"""Every period/parameter used by the Technical Indicator Engine.

Defaults are the conventional v1.0.0 periods requested for Phase 5. They are not optimised and
imply nothing about trading. Exact formulas: docs/technical-indicators.md.
"""

import hashlib
import json
from dataclasses import asdict, dataclass

# Bump when any formula or warm-up convention changes (parameters versioned via `fingerprint`).
ENGINE_VERSION = "1.0.0"


@dataclass(frozen=True)
class IndicatorConfig:
    sma_periods: tuple[int, ...] = (20, 50, 200)
    ema_periods: tuple[int, ...] = (20, 50, 200)
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14
    stoch_period: int = 14  # raw %K lookback
    stoch_k_smoothing: int = 3  # %K = SMA of raw %K
    stoch_d_period: int = 3  # %D = SMA of %K
    roc_period: int = 12
    atr_period: int = 14
    bollinger_period: int = 20
    bollinger_k: float = 2.0
    bollinger_ddof: int = 0  # population std (Bollinger's convention)
    realized_vol_period: int = 20  # number of log returns
    realized_vol_ddof: int = 1  # sample std (unbiased variance estimator)
    trading_days_per_year: int = 252  # annualisation for daily bars
    relative_volume_period: int = 20  # baseline = mean of the PREVIOUS n volumes

    def __post_init__(self) -> None:
        periods = {
            "macd_fast": self.macd_fast,
            "macd_slow": self.macd_slow,
            "macd_signal": self.macd_signal,
            "rsi_period": self.rsi_period,
            "stoch_period": self.stoch_period,
            "stoch_k_smoothing": self.stoch_k_smoothing,
            "stoch_d_period": self.stoch_d_period,
            "roc_period": self.roc_period,
            "atr_period": self.atr_period,
            "bollinger_period": self.bollinger_period,
            "realized_vol_period": self.realized_vol_period,
            "trading_days_per_year": self.trading_days_per_year,
            "relative_volume_period": self.relative_volume_period,
        }
        for name, value in periods.items():
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name, values in (("sma_periods", self.sma_periods), ("ema_periods", self.ema_periods)):
            if not values or any(not isinstance(p, int) or p < 1 for p in values):
                raise ValueError(f"{name} must be a non-empty tuple of positive integers")
            if len(set(values)) != len(values):
                raise ValueError(f"{name} contains duplicates")
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be < macd_slow")
        if self.bollinger_k <= 0:
            raise ValueError("bollinger_k must be > 0")
        if self.bollinger_ddof not in (0, 1) or self.realized_vol_ddof not in (0, 1):
            raise ValueError("ddof must be 0 or 1")
        if self.bollinger_period <= self.bollinger_ddof:
            raise ValueError("bollinger_period must exceed bollinger_ddof")
        if self.realized_vol_period <= self.realized_vol_ddof:
            raise ValueError("realized_vol_period must exceed realized_vol_ddof")

    def fingerprint(self) -> str:
        """Stable short hash of engine version + parameters (provenance for derived data)."""
        payload = json.dumps({"engine": ENGINE_VERSION, **asdict(self)}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
