"""Every threshold used by the Price Action Engine.

Defaults are explicit v1.0.0 CONVENTIONS, not optimised values, and carry no claim of
predictive value. Exact definitions: docs/price-action-engine.md.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, fields

# Bump when any algorithm changes (thresholds are versioned via `fingerprint`).
ENGINE_VERSION = "1.0.0"


@dataclass(frozen=True)
class PriceActionConfig:
    # ── swings (pivots) ──
    swing_left_bars: int = 5  # pivot must exceed the k bars before it (strict)
    swing_right_bars: int = 5  # ... and be >= the k bars after it -> confirmation latency k
    equal_tolerance_pct: float = 0.001  # |change| <= 0.1% vs previous pivot -> EH / EL
    # ── structure / trend ──
    structure_min_labels: int = 2  # last N high labels AND last N low labels must agree
    trend_quality_labels: int = 6  # labels (highs + lows) used for trend_quality
    # ── support / resistance zones ──
    zone_tolerance_pct: float = 0.005  # pivot joins a zone within 0.5% of its bounds
    zone_max_width_pct: float = 0.02  # a zone never grows wider than 2% of its midpoint
    # ── events ──
    breakout_min_distance_pct: float = 0.001  # close must clear the zone by >= 0.1%
    retest_window_bars: int = 10
    retest_tolerance_pct: float = 0.002  # low within 0.2% of the broken level counts as a touch
    outcome_window_bars: int = 10  # bars after a break during which failure is checked
    rejection_min_wick_ratio: float = 0.5
    rejection_min_distance_pct: float = 0.003
    sweep_min_distance_pct: float = 0.001
    # ── ranges / expansion ──
    range_window_bars: int = 20
    range_max_width_pct: float = 0.05  # window high-low <= 5% of its midpoint -> consolidation
    expansion_lookback: int = 20
    expansion_min_ratio: float = 1.5
    contraction_max_ratio: float = 0.5

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name.endswith(("_bars", "_labels", "_lookback")):
                if not isinstance(value, int) or value < 1:
                    raise ValueError(f"{f.name} must be a positive integer")
            elif f.name.endswith(("_pct", "_wick_ratio")):
                if not 0 < value < 1:
                    raise ValueError(f"{f.name} must be in (0, 1)")
            elif value <= 0:
                raise ValueError(f"{f.name} must be > 0")
        if self.contraction_max_ratio >= 1 or self.expansion_min_ratio <= 1:
            raise ValueError("need contraction_max_ratio < 1 < expansion_min_ratio")

    def fingerprint(self) -> str:
        """Stable short hash of engine version + thresholds (provenance for derived data)."""
        payload = json.dumps({"engine": ENGINE_VERSION, **asdict(self)}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
