"""Phase 9 validation configuration. Every choice here is fixed IN ADVANCE (ADR-0018).

Nothing in this module may be tuned to make any strategy look better: cost scenarios, slices,
block length, resample count, confidence level and seed are declared before results are seen.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date

VALIDATION_VERSION = "1.0.0"
METHODS = ("moving_block",)


@dataclass(frozen=True)
class SliceSpec:
    """Chronological slice [start, end] (inclusive dates; None = open). Slices are NOT
    statistically independent datasets and no model is trained on any of them."""

    name: str
    start: date | None = None
    end: date | None = None


@dataclass(frozen=True)
class CostScenario:
    """All-inclusive per-order cost: `bps` of notional (applied as slippage) + `per_order`."""

    name: str
    bps: float
    per_order: float
    spread_bps: float = 0.0  # used only to reproduce the Phase 8 default exactly

    def backtest_overrides(self) -> dict[str, float]:
        return {
            "slippage_bps": self.bps,
            "spread_bps": self.spread_bps,
            "commission_per_trade": self.per_order,
            "commission_bps": 0.0,
            "minimum_commission": 0.0,
        }


# Fixed scenarios approved at the Phase 8 gate (robustness, not optimisation).
COST_SCENARIOS: tuple[CostScenario, ...] = (
    CostScenario("A_zero", bps=0.0, per_order=0.0),
    CostScenario("B_5bps", bps=5.0, per_order=0.0),
    CostScenario("C_10bps", bps=10.0, per_order=0.0),
    # Phase 8 default: 2 bps slippage + half of a 2 bps spread (= 3 bps) + 1.00 per order
    CostScenario("D_default", bps=2.0, per_order=1.0, spread_bps=2.0),
)

DEFAULT_SLICES: tuple[SliceSpec, ...] = (
    SliceSpec("full"),
    SliceSpec("early", end=date(2009, 12, 31)),
    SliceSpec("late", start=date(2010, 1, 1)),
)


@dataclass(frozen=True)
class ValidationConfig:
    method: str = "moving_block"
    block_length: int = 21  # ~ one trading month; ~ n^(1/3) for n = 8,479 sessions
    n_resamples: int = 2000
    confidence: float = 0.95
    seed: int = 20261007
    periods_per_year: int = 252
    slices: tuple[SliceSpec, ...] = field(default=DEFAULT_SLICES)
    cost_scenarios: tuple[CostScenario, ...] = field(default=COST_SCENARIOS)
    default_scenario: str = "D_default"
    formal_slice: str = "full"  # the only slice on which the formal test family is evaluated

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise ValueError(f"method must be one of {METHODS}")
        if not isinstance(self.block_length, int) or self.block_length < 1:
            raise ValueError("block_length must be a positive integer")
        if not isinstance(self.n_resamples, int) or self.n_resamples < 100:
            raise ValueError("n_resamples must be an integer >= 100")
        if not 0.5 < self.confidence < 1:
            raise ValueError("confidence must be in (0.5, 1)")
        names = [s.name for s in self.slices]
        if not names or len(names) != len(set(names)):
            raise ValueError("slices must be non-empty with unique names")
        for s in self.slices:
            if s.start and s.end and s.start > s.end:
                raise ValueError(f"slice {s.name}: start > end")
        scen = [c.name for c in self.cost_scenarios]
        if len(scen) != len(set(scen)) or self.default_scenario not in scen:
            raise ValueError("cost scenarios must be unique and include the default scenario")
        if any(c.bps < 0 or c.per_order < 0 or c.spread_bps < 0 for c in self.cost_scenarios):
            raise ValueError("cost scenario values must be >= 0")
        if self.formal_slice not in names:
            raise ValueError("formal_slice must be one of the slices")

    def fingerprint(self) -> str:
        payload = json.dumps(
            {"version": VALIDATION_VERSION, **asdict(self)}, sort_keys=True, default=str
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
