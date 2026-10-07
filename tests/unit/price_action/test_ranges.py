import dataclasses

import pandas as pd
import pytest

from app.price_action.config import PriceActionConfig
from app.price_action.ranges import compute_ranges
from tests.unit.candles.helpers import Candle, bars

CFG = dataclasses.replace(PriceActionConfig(), range_window_bars=5, expansion_lookback=5)


def ranges(candles: list[Candle], cfg: PriceActionConfig = CFG) -> pd.DataFrame:
    b = bars(candles)
    return compute_ranges(b, b["high"] - b["low"], cfg)


TIGHT: list[Candle] = [(100, 101, 99.5, 100.5)] * 8  # window width 1.5/100.25 = 1.5%


def test_consolidation_episode() -> None:
    r = ranges([(90, 95, 85, 92), *TIGHT])
    assert not r["in_consolidation"].iloc[:5].any()  # first window includes the wide bar
    last = r.iloc[-1]
    assert last["in_consolidation"]
    assert last["range_high"] == 101 and last["range_low"] == 99.5
    assert last["range_start"] == r.index[1]  # first bar of the window of bar 5 (bars 1..5)
    assert last["range_bar_count"] == 8  # bars 1..8
    assert last["range_width_pct"] == pytest.approx(1.5 / 100.25)


def test_insufficient_history_is_not_consolidation() -> None:
    r = ranges(TIGHT[:4])
    assert not r["in_consolidation"].any()
    assert r["window_width_pct"].isna().all()


def test_width_boundary_is_inclusive() -> None:
    # high 102.5, low 97.5 -> width 5 / 100 = 0.05 exactly
    r = ranges([(100, 102.5, 97.5, 100)] * 5)
    assert r["in_consolidation"].iloc[-1]
    r2 = ranges([(100, 102.51, 97.5, 100)] * 5)
    assert not r2["in_consolidation"].iloc[-1]


def test_candle_range_expansion_and_contraction() -> None:
    base: list[Candle] = [(100, 101, 99, 100)] * 5  # ranges of 2
    r = ranges([*base, (100, 103, 100, 102.5)])  # range 3 = 1.5x -> expansion (inclusive)
    assert r["candle_range_state"].iloc[-1] == "expansion"
    assert r["candle_range_ratio"].iloc[-1] == 1.5
    r = ranges([*base, (100, 100.5, 99.5, 100)])  # range 1 = 0.5x -> contraction
    assert r["candle_range_state"].iloc[-1] == "contraction"
    r = ranges([*base, (100, 101.2, 99, 100)])
    assert r["candle_range_state"].iloc[-1] == "normal"
    assert r["candle_range_state"].iloc[0] == "unknown"


def test_expansion_magnitude_is_separate_from_direction() -> None:
    base: list[Candle] = [(100, 101, 99, 100)] * 5
    up = ranges([*base, (100, 103, 99.9, 102.9)])
    down = ranges([*base, (103, 103.1, 100, 100.1)])
    assert up["candle_range_state"].iloc[-1] == down["candle_range_state"].iloc[-1] == "expansion"


def test_window_width_expansion() -> None:
    quiet: list[Candle] = [(100, 100.5, 99.5, 100)] * 5  # width 1%
    wide: list[Candle] = [(100, 101, 99, 100)] * 5  # width 2% -> ratio 2.0
    r = ranges([*quiet, *wide])
    assert r["window_width_state"].iloc[-1] == "expansion"
    assert r["window_direction"].iloc[-1] == "flat"
