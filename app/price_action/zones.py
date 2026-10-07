"""Support/resistance ZONES built incrementally from confirmed pivots (point-in-time).

A zone is a price interval [low, high] supported by one or more confirmed pivots:
- A newly confirmed pivot at price p joins the closest active zone whose bounds, widened by
  tol = p * zone_tolerance_pct, contain p — unless joining would make the zone wider than
  zone_max_width_pct of its midpoint, in which case it starts a new zone.
- After a zone grows, any zone overlapping it (within tolerance) is merged into the older one,
  again only if the union respects the width cap.
- A zone becomes AVAILABLE when its first pivot is confirmed (`first_seen`), never earlier.
- A touch is a later bar whose [low, high] intersects the zone.
The zone's role (support/resistance) is not stored: it depends on where price is at query time.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

ZONE_COLUMNS = (
    "zone_id",
    "zone_low",
    "zone_high",
    "zone_mid",
    "width_pct",
    "evidence_count",
    "source_type",
    "first_pivot_ts",
    "first_seen",
    "last_tested",
    "touch_count",
)


@dataclass
class Zone:
    zone_id: int
    low: float
    high: float
    evidence_count: int
    kinds: set[str]
    first_pivot_idx: int
    first_seen_idx: int
    last_tested_idx: int | None = None
    touch_count: int = 0
    active: bool = True
    pivot_ids: list[int] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2

    def width_after(self, low: float, high: float) -> float:
        lo, hi = min(self.low, low), max(self.high, high)
        return (hi - lo) / ((hi + lo) / 2)


class ZoneBook:
    def __init__(self, tolerance_pct: float, max_width_pct: float) -> None:
        self.tolerance_pct = tolerance_pct
        self.max_width_pct = max_width_pct
        self.zones: list[Zone] = []
        self._cache: tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None = None

    # ── queries ──

    def arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """(zone_ids, lows, highs, first_seen_idx) of active zones, cached until a change."""
        if self._cache is None:
            active = [z for z in self.zones if z.active]
            self._cache = (
                np.array([z.zone_id for z in active], dtype=np.int64),
                np.array([z.low for z in active], dtype=np.float64),
                np.array([z.high for z in active], dtype=np.float64),
                np.array([z.first_seen_idx for z in active], dtype=np.int64),
            )
        return self._cache

    def get(self, zone_id: int) -> Zone:
        return self.zones[zone_id]

    # ── updates ──

    def add_pivot(
        self, price: float, kind: str, pivot_idx: int, swing_id: int, now_idx: int
    ) -> Zone:
        tol = price * self.tolerance_pct
        candidates = [
            z
            for z in self.zones
            if z.active
            and z.low - tol <= price <= z.high + tol
            and z.width_after(price, price) <= self.max_width_pct
        ]
        if candidates:
            zone = min(candidates, key=lambda z: (abs(z.mid - price), z.zone_id))
            zone.low, zone.high = min(zone.low, price), max(zone.high, price)
            zone.evidence_count += 1
            zone.kinds.add(kind)
            zone.pivot_ids.append(swing_id)
            self._merge_overlaps(zone)
        else:
            zone = Zone(
                zone_id=len(self.zones),
                low=price,
                high=price,
                evidence_count=1,
                kinds={kind},
                first_pivot_idx=pivot_idx,
                first_seen_idx=now_idx,
                pivot_ids=[swing_id],
            )
            self.zones.append(zone)
        self._cache = None
        return zone

    def _merge_overlaps(self, zone: Zone) -> None:
        merged = True
        while merged:
            merged = False
            tol = zone.mid * self.tolerance_pct
            for other in self.zones:
                if other is zone or not other.active:
                    continue
                overlaps = other.low <= zone.high + tol and other.high >= zone.low - tol
                if overlaps and zone.width_after(other.low, other.high) <= self.max_width_pct:
                    keep, drop = (zone, other) if zone.zone_id < other.zone_id else (other, zone)
                    keep.low, keep.high = min(keep.low, drop.low), max(keep.high, drop.high)
                    keep.evidence_count += drop.evidence_count
                    keep.kinds |= drop.kinds
                    keep.pivot_ids = sorted(keep.pivot_ids + drop.pivot_ids)
                    keep.first_pivot_idx = min(keep.first_pivot_idx, drop.first_pivot_idx)
                    keep.first_seen_idx = min(keep.first_seen_idx, drop.first_seen_idx)
                    tested = [
                        i for i in (keep.last_tested_idx, drop.last_tested_idx) if i is not None
                    ]
                    keep.last_tested_idx = max(tested) if tested else None
                    keep.touch_count += drop.touch_count
                    drop.active = False
                    zone = keep
                    merged = True
                    break

    def record_touches(self, idx: int, bar_low: float, bar_high: float) -> None:
        """Zones available BEFORE bar idx whose bounds intersect the bar's range."""
        ids, lows, highs, first = self.arrays()
        hit = (first < idx) & (lows <= bar_high) & (highs >= bar_low)
        for zid in ids[hit]:
            z = self.zones[int(zid)]
            z.touch_count += 1
            z.last_tested_idx = idx

    # ── export ──

    def to_frame(self, index: pd.DatetimeIndex) -> pd.DataFrame:
        rows = [
            {
                "zone_id": z.zone_id,
                "zone_low": z.low,
                "zone_high": z.high,
                "zone_mid": z.mid,
                "width_pct": round((z.high - z.low) / z.mid, 10),
                "evidence_count": z.evidence_count,
                "source_type": "mixed" if len(z.kinds) > 1 else f"swing_{next(iter(z.kinds))}",
                "first_pivot_ts": index[z.first_pivot_idx],
                "first_seen": index[z.first_seen_idx],
                "last_tested": None if z.last_tested_idx is None else index[z.last_tested_idx],
                "touch_count": z.touch_count,
            }
            for z in self.zones
            if z.active
        ]
        return pd.DataFrame(rows, columns=list(ZONE_COLUMNS))
