"""Paper-trading configuration. Historical replay only; no live mode, no broker, no network."""

import hashlib
import json
from dataclasses import asdict, dataclass, field

from app.backtest.config import BacktestConfig
from app.risk.config import SCENARIOS, RiskConfig

PAPER_VERSION = "1.0.0"
MODE = "historical_replay"  # the only mode that exists


@dataclass(frozen=True)
class PaperConfig:
    # Operational default (Phase 11 gate): no overlay + the hard safety limits below.
    risk: RiskConfig = field(default_factory=lambda: SCENARIOS[0])
    # Phase 8 execution/cost assumptions (next-session open, raw prices, default costs).
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    # Hard safety limits enforced by the broker independently of the risk layer.
    max_gross_exposure: float = 1.0
    cash_tolerance: float = 1e-6  # absolute; resulting cash below -tolerance is rejected

    def __post_init__(self) -> None:
        if not 0 < self.max_gross_exposure <= 1:
            raise ValueError("max_gross_exposure must be in (0, 1] (no leverage)")
        if self.risk.limits.max_gross_exposure > self.max_gross_exposure:
            raise ValueError("risk limits must not exceed the broker's hard exposure limit")
        if self.cash_tolerance < 0:
            raise ValueError("cash_tolerance must be >= 0")

    def fingerprint(self) -> str:
        payload = json.dumps(
            {"paper": PAPER_VERSION, "mode": MODE, **asdict(self)}, sort_keys=True, default=str
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
