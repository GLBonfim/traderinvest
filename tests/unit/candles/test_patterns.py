"""Positive, negative and borderline cases for every detector (synthetic candles)."""

import pytest

from app.candles.patterns import REGISTRY
from tests.unit.candles.helpers import (
    Candle,
    analyze,
    observation,
    patterns_at_last,
    trend_prefix,
)

DOWN = trend_prefix("down", end_close=106)
UP = trend_prefix("up", end_close=106)
FLAT = trend_prefix("flat", end_close=106)

# Every threshold met EXACTLY: lower wick 6/10 = 0.60, upper wick 1/10 = 0.10,
# body 3/10 = 0.30, lower wick 6 = 2.0 x body 3.
HAMMER_AT_THRESHOLDS: Candle = (106, 110, 100, 109)
HAMMER_CLEAR: Candle = (106, 106.6, 103, 106.5)
INVERTED_AT_THRESHOLDS: Candle = (101, 110, 100, 104)  # mirror of the above


def test_registry_has_the_twenty_required_patterns() -> None:
    names = {spec.name for spec, _ in REGISTRY}
    assert names == {
        "hammer", "inverted_hammer", "shooting_star", "hanging_man", "bullish_engulfing",
        "bearish_engulfing", "morning_star", "evening_star", "tweezer_top", "tweezer_bottom",
        "three_white_soldiers", "three_black_crows", "rising_three_methods",
        "falling_three_methods", "inside_bar", "outside_bar", "doji", "spinning_top",
        "marubozu", "long_body",
    }  # fmt: skip


# ── hammer family: same shape, identity decided by the PRIOR trend ──


def test_hammer_after_downtrend() -> None:
    assert "hammer" in patterns_at_last([*DOWN, HAMMER_CLEAR])
    assert "hanging_man" not in patterns_at_last([*DOWN, HAMMER_CLEAR])


def test_same_shape_after_uptrend_is_hanging_man() -> None:
    found = patterns_at_last([*UP, HAMMER_CLEAR])
    assert "hanging_man" in found and "hammer" not in found


def test_hammer_shape_without_trend_is_neither() -> None:
    assert not {"hammer", "hanging_man"} & patterns_at_last([*FLAT, HAMMER_CLEAR])


def test_hammer_shape_without_history_is_neither() -> None:
    assert not {"hammer", "hanging_man"} & patterns_at_last([HAMMER_CLEAR])


def test_hammer_at_exact_thresholds_is_included_with_zero_strength() -> None:
    obs = observation([*DOWN, HAMMER_AT_THRESHOLDS], "hammer")
    assert obs["strength"] == 0.0
    assert obs["context_requirements_met"]
    assert obs["prior_trend"] == "down"


@pytest.mark.parametrize(
    "candle",
    [
        (106, 110, 100.01, 109),  # lower wick ratio just below 0.60
        (106, 110.01, 100, 109),  # upper wick ratio just above 0.10
        (106, 110, 100, 109.01),  # body ratio just above 0.30 (and wick < 2 x body)
    ],
)
def test_hammer_just_outside_thresholds_is_rejected(candle: Candle) -> None:
    assert "hammer" not in patterns_at_last([*DOWN, candle])


def test_inverted_hammer_and_shooting_star() -> None:
    assert "inverted_hammer" in patterns_at_last([*DOWN, INVERTED_AT_THRESHOLDS])
    found_up = patterns_at_last([*UP, INVERTED_AT_THRESHOLDS])
    assert "shooting_star" in found_up and "inverted_hammer" not in found_up
    assert "inverted_hammer" not in patterns_at_last([*DOWN, (101, 110, 99.99, 104)])


def test_orientation_is_conventional_label() -> None:
    assert observation([*DOWN, HAMMER_CLEAR], "hammer")["orientation"] == "bullish"
    assert observation([*UP, HAMMER_CLEAR], "hanging_man")["orientation"] == "bearish"


# ── engulfing ──

PREV_BEAR: Candle = (105, 105.5, 102.5, 103)
BULL_ENGULF: Candle = (102.8, 105.8, 102.5, 105.5)


def test_bullish_engulfing_with_and_without_context() -> None:
    with_ctx = observation(
        [*trend_prefix("down", 104), PREV_BEAR, BULL_ENGULF], "bullish_engulfing"
    )
    assert with_ctx["context_requirements_met"]
    assert with_ctx["bars_in_pattern"] == 2
    no_ctx = observation([*trend_prefix("flat", 104), PREV_BEAR, BULL_ENGULF], "bullish_engulfing")
    assert not no_ctx["context_requirements_met"]  # shape kept; context flagged


def test_bullish_engulfing_negatives() -> None:
    assert "bullish_engulfing" not in patterns_at_last([PREV_BEAR, (102.8, 105.8, 102.5, 104.9)])
    # body exactly equal to previous body: not engulfing (strictly larger required)
    assert "bullish_engulfing" not in patterns_at_last([PREV_BEAR, (103, 105.5, 102.5, 105)])
    # previous candle is a doji: excluded
    assert "bullish_engulfing" not in patterns_at_last([(105, 106, 102, 104.9), BULL_ENGULF])


def test_engulfing_strength_is_log_scaled_body_multiple() -> None:
    # previous body 2.0, current body 6.0 -> 3x -> log(3) / log(10)
    obs = observation([(105, 105.5, 102.5, 103), (102, 108.2, 101.9, 108)], "bullish_engulfing")
    assert obs["strength"] == pytest.approx(0.477121, abs=1e-6)


def test_bearish_engulfing() -> None:
    prev_bull: Candle = (103, 105.5, 102.5, 105)
    cur: Candle = (105.2, 105.5, 102.2, 102.5)
    obs = observation([*trend_prefix("up", 102), prev_bull, cur], "bearish_engulfing")
    assert obs["context_requirements_met"] and obs["orientation"] == "bearish"
    assert "bearish_engulfing" not in patterns_at_last([prev_bull, (105.2, 105.5, 102.2, 103.1)])


# ── stars ──

MORNING: list[Candle] = [
    (110, 110.5, 103.5, 104),
    (103.5, 104.5, 102.5, 103.8),
    (104, 109, 103.8, 108.5),
]
EVENING: list[Candle] = [
    (100, 106.5, 99.5, 106),
    (106.5, 107.5, 105.5, 106.2),
    (106, 106.2, 101, 101.5),
]


def test_morning_star() -> None:
    obs = observation([*trend_prefix("down", 111), *MORNING], "morning_star")
    assert obs["bars_in_pattern"] == 3 and obs["context_requirements_met"]
    assert obs["first_bar_ts"] < obs["ts"]


def test_morning_star_insufficient_penetration() -> None:
    weak = [*MORNING[:2], (104, 107, 103.8, 106.5)]  # retraces 41.7% < 50%
    assert "morning_star" not in patterns_at_last(weak)


def test_morning_star_star_body_must_be_below_first_close() -> None:
    bad = [MORNING[0], (104.2, 105, 103.5, 104.5), MORNING[2]]
    assert "morning_star" not in patterns_at_last(bad)


def test_evening_star() -> None:
    obs = observation([*trend_prefix("up", 99), *EVENING], "evening_star")
    assert obs["context_requirements_met"] and obs["orientation"] == "bearish"
    assert "evening_star" not in patterns_at_last([*EVENING[:2], (106, 106.2, 103, 103.5)])


def test_star_requires_three_bars_of_history() -> None:
    assert "morning_star" not in patterns_at_last(MORNING[1:])


# ── tweezers ──


def test_tweezer_bottom_and_top() -> None:
    bottom = [(105, 105.5, 101, 102), (102, 105, 101.05, 104.5)]
    assert "tweezer_bottom" in patterns_at_last(bottom)
    assert "tweezer_bottom" not in patterns_at_last([bottom[0], (102, 105, 101.5, 104.5)])
    top = [(100, 104, 99.5, 103), (103, 104.05, 100, 100.5)]
    assert "tweezer_top" in patterns_at_last(top)
    assert "tweezer_top" not in patterns_at_last([top[0], (103, 104.6, 100, 100.5)])


# ── three soldiers / crows ──

SOLDIERS: list[Candle] = [
    (100, 103.3, 99.8, 103),
    (102, 105.3, 101.8, 105),
    (104, 107.3, 103.8, 107),
]
CROWS: list[Candle] = [(107, 107.2, 103.7, 104), (105, 105.2, 101.7, 102), (103, 103.2, 99.7, 100)]


def test_three_white_soldiers() -> None:
    assert "three_white_soldiers" in patterns_at_last(SOLDIERS)
    gap_open = [*SOLDIERS[:2], (105.5, 107.3, 105.4, 107)]  # opens above previous body
    assert "three_white_soldiers" not in patterns_at_last(gap_open)
    long_wick = [*SOLDIERS[:2], (104, 108.5, 103.8, 107)]  # does not close near the high
    assert "three_white_soldiers" not in patterns_at_last(long_wick)


def test_three_black_crows() -> None:
    assert "three_black_crows" in patterns_at_last(CROWS)
    assert "three_black_crows" not in patterns_at_last([*CROWS[:2], (103, 103.2, 101.7, 102.1)])


# ── three methods ──

RISING: list[Candle] = [
    (100, 106.5, 99.5, 106),
    (105, 105.5, 103.5, 104),
    (104, 104.5, 102.5, 103),
    (103, 103.5, 102, 102.5),
    (102.5, 108, 102.3, 107.5),
]
FALLING: list[Candle] = [
    (106, 106.5, 99.5, 100),
    (101, 102.5, 100.5, 102),
    (102, 103.5, 101.5, 103),
    (103, 104, 102.5, 103.5),
    (103.5, 103.7, 98, 98.5),
]


def test_rising_three_methods() -> None:
    obs = observation(RISING, "rising_three_methods")
    assert obs["bars_in_pattern"] == 5
    escaped = [RISING[0], (105, 107, 103.5, 104), *RISING[2:]]  # inner high above candle 1
    assert "rising_three_methods" not in patterns_at_last(escaped)
    no_break = [*RISING[:4], (102.5, 106, 102.3, 105.9)]  # final close not above candle 1
    assert "rising_three_methods" not in patterns_at_last(no_break)


def test_falling_three_methods() -> None:
    assert "falling_three_methods" in patterns_at_last(FALLING)
    assert "falling_three_methods" not in patterns_at_last(
        [*FALLING[:4], (103.5, 103.7, 99.8, 100.2)]
    )


def test_three_methods_requires_five_bars() -> None:
    assert "rising_three_methods" not in patterns_at_last(RISING[1:])


# ── structure ──


def test_inside_and_outside_bar() -> None:
    prev: Candle = (101, 105, 100, 104)
    assert "inside_bar" in patterns_at_last([prev, (102, 104, 101, 103)])
    assert "inside_bar" not in patterns_at_last([prev, (102, 105, 101, 103)])  # equal high
    assert "outside_bar" in patterns_at_last([prev, (102, 106, 99, 103)])
    assert "outside_bar" not in patterns_at_last([prev, (102, 105, 99, 103)])  # equal high
    assert not {"inside_bar", "outside_bar"} & patterns_at_last([(102, 104, 101, 103)])


# ── indecision / momentum ──


def test_doji_boundary() -> None:
    assert observation([(100, 101, 99, 100.2)], "doji")["strength"] == 0.0  # 0.2/2 = 0.10
    just_above = patterns_at_last([(100, 101, 99, 100.21)])
    assert "doji" not in just_above
    assert "spinning_top" in just_above


def test_spinning_top() -> None:
    assert "spinning_top" in patterns_at_last([(100, 101.5, 99, 100.5)])
    assert "spinning_top" not in patterns_at_last([(100, 100.6, 99, 100.5)])  # tiny upper wick


def test_marubozu_orientation_follows_candle() -> None:
    assert observation([(100, 105.1, 99.9, 105)], "marubozu")["orientation"] == "bullish"
    assert observation([(105, 105.1, 99.9, 100)], "marubozu")["orientation"] == "bearish"
    assert "marubozu" not in patterns_at_last([(100, 105.5, 99.9, 105)])


def test_long_body_needs_history_and_size() -> None:
    flat = trend_prefix("flat", 100, n=25)  # bodies of 0.5
    assert "long_body" in patterns_at_last([*flat, (100, 102.6, 99.9, 102.5)])  # 5x
    assert "long_body" in patterns_at_last([*flat, (100, 100.8, 99.95, 100.75)])  # exactly 1.5x
    assert "long_body" not in patterns_at_last([*flat, (100, 100.8, 99.95, 100.7)])  # 1.4x
    assert "long_body" not in patterns_at_last([*flat[-10:], (100, 102.6, 99.9, 102.5)])


# ── combinations ──


def test_multiple_patterns_on_one_candle() -> None:
    # Hammer with a tiny body is also a doji; the engine does not force one label.
    candle: Candle = (106, 106.1, 103, 106.05)
    found = patterns_at_last([*DOWN, candle])
    assert {"hammer", "doji"} <= found


def test_every_pattern_detected_somewhere() -> None:
    seen: set[str] = set()
    for candles in (
        [*DOWN, HAMMER_CLEAR], [*UP, HAMMER_CLEAR], [*DOWN, INVERTED_AT_THRESHOLDS],
        [*UP, INVERTED_AT_THRESHOLDS], [PREV_BEAR, BULL_ENGULF],
        [(103, 105.5, 102.5, 105), (105.2, 105.5, 102.2, 102.5)], MORNING, EVENING,
        [(105, 105.5, 101, 102), (102, 105, 101.05, 104.5)],
        [(100, 104, 99.5, 103), (103, 104.05, 100, 100.5)], SOLDIERS, CROWS, RISING, FALLING,
        [(101, 105, 100, 104), (102, 104, 101, 103)], [(101, 105, 100, 104), (102, 106, 99, 103)],
        [(100, 101, 99, 100.1)], [(100, 101.5, 99, 100.5)], [(100, 105.1, 99.9, 105)],
        [*trend_prefix("flat", 100, n=25), (100, 102.6, 99.9, 102.5)],
    ):  # fmt: skip
        seen |= set(analyze(candles).observations["pattern"])
    assert seen == {spec.name for spec, _ in REGISTRY}


def test_strength_is_bounded() -> None:
    obs = analyze([*DOWN, HAMMER_CLEAR, *SOLDIERS, *MORNING]).observations
    assert obs["strength"].between(0, 1).all()
