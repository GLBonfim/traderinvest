"""Every numerical threshold used by the Candlestick Engine lives here.

The defaults are explicit CONVENTIONS chosen to make textbook descriptions ("small body",
"long wick") precise. They are not optimised and carry no claim of predictive value; later
phases may study sensitivity to them. Exact definitions: docs/candlestick-engine.md.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, fields

# Bump when any detector's logic changes (thresholds are versioned via `fingerprint`).
ENGINE_VERSION = "1.0.0"


@dataclass(frozen=True)
class CandleConfig:
    # ── single-candle shape ──
    doji_max_body_ratio: float = 0.10
    spinning_top_max_body_ratio: float = 0.30
    spinning_top_min_wick_ratio: float = 0.25
    marubozu_min_body_ratio: float = 0.90
    marubozu_max_wick_ratio: float = 0.05
    # hammer / hanging man (long LOWER wick); mirrored for inverted hammer / shooting star
    hammer_max_body_ratio: float = 0.30
    hammer_min_long_wick_ratio: float = 0.60
    hammer_max_short_wick_ratio: float = 0.10
    hammer_min_wick_body_multiple: float = 2.0
    # ── body size classes used by multi-candle patterns ──
    large_body_min_ratio: float = 0.50
    small_body_max_ratio: float = 0.30
    # long body candle: large relative to the candle's own range AND to recent bodies
    long_body_lookback: int = 20
    long_body_min_multiple: float = 1.5
    long_body_min_body_ratio: float = 0.60
    # ── multi-candle specifics ──
    # engulfing strength: log-scaled body multiple, 1x -> 0, this multiple -> 1 (heavy-tailed:
    # SPY engulfing body multiples have median ~3x, p90 ~8x; a linear 3x cap saturated)
    engulfing_ideal_body_multiple: float = 10.0
    star_min_penetration: float = 0.50  # third candle retraces >= 50% of the first body
    tweezer_max_diff_range_frac: float = 0.05  # |extreme1 - extreme2| / mean(range1, range2)
    soldiers_max_closing_wick_ratio: float = 0.25  # soldiers close near high, crows near low
    # ── context ──
    trend_lookback: int = 10
    trend_min_score: float = 1.0  # net move >= 1 average range over the lookback
    relative_volume_lookback: int = 20

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name.endswith("_lookback"):
                if not isinstance(value, int) or value < 1:
                    raise ValueError(f"{f.name} must be a positive integer")
            elif f.name.endswith(("_ratio", "_frac", "_penetration")):
                if not 0 <= value <= 1:
                    raise ValueError(f"{f.name} must be in [0, 1]")
            elif value <= 0:
                raise ValueError(f"{f.name} must be > 0")
        if self.doji_max_body_ratio >= self.spinning_top_max_body_ratio:
            raise ValueError("doji_max_body_ratio must be < spinning_top_max_body_ratio")
        if self.engulfing_ideal_body_multiple <= 1:
            raise ValueError("engulfing_ideal_body_multiple must be > 1")
        if self.small_body_max_ratio >= self.large_body_min_ratio:
            raise ValueError("small_body_max_ratio must be < large_body_min_ratio")

    def fingerprint(self) -> str:
        """Stable short hash of engine version + thresholds (provenance for derived data)."""
        payload = json.dumps({"engine": ENGINE_VERSION, **asdict(self)}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]
