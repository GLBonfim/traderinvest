from app.price_action.engine import PriceActionEngine
from app.price_action.zones import ZoneBook
from tests.unit.candles.helpers import bars
from tests.unit.price_action.helpers import BASE, FAST


def book() -> ZoneBook:
    return ZoneBook(tolerance_pct=0.005, max_width_pct=0.02)


def test_single_pivot_creates_point_zone() -> None:
    b = book()
    z = b.add_pivot(100.0, "high", pivot_idx=3, swing_id=0, now_idx=5)
    assert (z.low, z.high, z.evidence_count, z.first_seen_idx) == (100.0, 100.0, 1, 5)


def test_clustered_pivots_join_one_zone() -> None:
    b = book()
    b.add_pivot(100.0, "high", 3, 0, 5)
    z = b.add_pivot(100.4, "low", 8, 1, 10)
    assert (z.zone_id, z.low, z.high, z.evidence_count) == (0, 100.0, 100.4, 2)
    assert z.kinds == {"high", "low"}
    assert b.to_frame(bars([(1, 1, 1, 1)] * 11).index)["source_type"].tolist() == ["mixed"]


def test_tolerance_boundary() -> None:
    inside = book()
    inside.add_pivot(100.0, "high", 0, 0, 0)
    assert inside.add_pivot(100.5, "high", 1, 1, 1).zone_id == 0  # 0.5 <= 100.5 * 0.005
    outside = book()
    outside.add_pivot(100.0, "high", 0, 0, 0)
    assert outside.add_pivot(100.51, "high", 1, 1, 1).zone_id == 1  # 0.51 > 0.50255


def test_width_cap_starts_new_zone() -> None:
    b = book()
    b.add_pivot(100.0, "high", 0, 0, 0)
    b.add_pivot(100.5, "high", 1, 1, 1)
    b.add_pivot(101.0, "high", 2, 2, 2)
    b.add_pivot(101.5, "high", 3, 3, 3)  # zone would reach 1.5%: still allowed
    z = b.add_pivot(102.1, "high", 4, 4, 4)  # 2.08% wide -> refused, new zone
    assert z.zone_id == 1
    assert all((zz.high - zz.low) / zz.mid <= 0.02 for zz in b.zones)


def test_growing_zone_merges_overlapping_neighbour() -> None:
    b = book()
    b.add_pivot(100.0, "high", 0, 0, 0)
    b.add_pivot(100.9, "low", 1, 1, 1)  # 0.9% away: separate zone
    assert len([z for z in b.zones if z.active]) == 2
    b.add_pivot(100.45, "high", 2, 2, 2)  # joins zone 0, which now overlaps zone 1
    active = [z for z in b.zones if z.active]
    assert len(active) == 1
    assert (active[0].zone_id, active[0].low, active[0].high, active[0].evidence_count) == (
        0, 100.0, 100.9, 3,
    )  # fmt: skip


def test_touches_ignore_zones_not_yet_available() -> None:
    b = book()
    b.add_pivot(100.0, "high", 0, 0, now_idx=5)
    b.record_touches(5, 99.0, 101.0)  # same bar the zone appears: not a touch
    assert b.zones[0].touch_count == 0
    b.record_touches(6, 99.0, 101.0)
    assert (b.zones[0].touch_count, b.zones[0].last_tested_idx) == (1, 6)
    b.record_touches(7, 101.0, 102.0)  # no intersection
    assert b.zones[0].touch_count == 1


def test_zone_first_seen_is_confirmation_not_pivot_time() -> None:
    a = PriceActionEngine(FAST).analyze(bars(BASE))
    z = a.zones[a.zones["zone_high"] == 110.0].iloc[0]
    assert z["first_pivot_ts"] == a.state.index[4]
    assert z["first_seen"] == a.state.index[6]
    # Before confirmation the 110 zone is not visible as resistance.
    assert a.state.iloc[5]["resistance_high"] != 110.0
    assert a.state.iloc[6]["resistance_high"] == 110.0
