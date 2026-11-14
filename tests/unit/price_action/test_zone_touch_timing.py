"""Zone touches must use bounds known before the touching bar."""

import pandas as pd
import pytest

from app.price_action.config import PriceActionConfig
from app.price_action.engine import PriceActionEngine
from tests.unit.price_action.helpers import hl_bars


@pytest.mark.parametrize("mirror", [False, True])
def test_newly_confirmed_pivot_cannot_expand_same_bar_touch(mirror: bool) -> None:
    bars = hl_bars(
        [99.0, 100.0, 99.0, 99.8, 100.4, 100.35, 100.32],
        [98.95, 99.95, 98.95, 99.75, 100.35, 100.30, 100.31],
    )
    if mirror:
        original = bars.copy()
        for col, source in [("open", "open"), ("high", "low"), ("low", "high"), ("close", "close")]:
            bars[col] = 200.0 - original[source]
    engine = PriceActionEngine(PriceActionConfig(swing_left_bars=1, swing_right_bars=1))
    # The 100 zone exists from bar 2; bar 5 confirms the pivot from bar 4,
    # expanding that zone to 100.4 (or 99.6 when mirrored). Bar 5 only
    # intersects the expanded portion, whose bounds were not known at bar 4.
    prefix = engine.analyze(bars.iloc[:6])
    zone = prefix.zones[prefix.zones["evidence_count"] == 2].iloc[0]
    assert zone["touch_count"] == 0
    assert pd.isna(zone["last_tested"])
    assert zone["first_seen"] == bars.index[2]
    # The following bar may touch those now-known bounds.
    full = engine.analyze(bars)
    zone = full.zones[full.zones["zone_id"] == zone["zone_id"]].iloc[0]
    assert zone["touch_count"] == 1
    assert zone["last_tested"] == bars.index[6]
    pd.testing.assert_frame_equal(full.state.iloc[:6], prefix.state)
    pd.testing.assert_frame_equal(
        full.events[full.events["available_at"] <= bars.index[5]].reset_index(drop=True),
        prefix.events,
    )
