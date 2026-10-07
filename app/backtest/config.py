"""Backtest configuration: execution convention and explicit cost ASSUMPTIONS.

Defaults are transparent research assumptions, not broker quotes, and were not calibrated on
any historical result. Definitions: docs/backtesting.md.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date

# Bump when execution, accounting or metric conventions change.
ENGINE_VERSION = "1.0.0"
EXECUTION_MODELS = ("next_session_open",)


@dataclass(frozen=True)
class BacktestConfig:
    initial_capital: float = 100_000.0  # normalised research capital, not a recommendation
    execution: str = "next_session_open"
    # ── costs per order (each entry and each exit is one order) ──
    commission_per_trade: float = 1.0  # flat amount per order (generic assumption)
    commission_bps: float = 0.0  # proportional commission on notional
    minimum_commission: float = 0.0
    spread_bps: float = 2.0  # FULL quoted spread; half is paid on each order
    slippage_bps: float = 2.0  # per order, adverse
    # ── conventions ──
    periods_per_year: int = 252  # annualisation for daily sessions
    risk_free_rate: float = 0.0  # per-period; research convention (no point-in-time series yet)
    # ── temporal slicing (metadata only; no optimisation) ──
    start: date | None = None
    end: date | None = None
    period_label: str | None = None  # e.g. "train" / "validation" / "test" (label only)

    def __post_init__(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be > 0")
        if self.execution not in EXECUTION_MODELS:
            raise ValueError(f"execution must be one of {EXECUTION_MODELS}")
        for name in (
            "commission_per_trade",
            "commission_bps",
            "minimum_commission",
            "spread_bps",
            "slippage_bps",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be >= 0")
        if self.periods_per_year < 1:
            raise ValueError("periods_per_year must be >= 1")
        if self.start and self.end and self.start > self.end:
            raise ValueError("start must be <= end")

    def without_costs(self) -> "BacktestConfig":
        return BacktestConfig(
            **{**asdict(self), "commission_per_trade": 0.0, "commission_bps": 0.0,
               "minimum_commission": 0.0, "spread_bps": 0.0, "slippage_bps": 0.0}
        )  # fmt: skip

    def fingerprint(self) -> str:
        payload = json.dumps(
            {"engine": ENGINE_VERSION, **asdict(self)}, sort_keys=True, default=str
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
