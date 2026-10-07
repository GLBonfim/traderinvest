import math

import numpy as np
import pandas as pd
import pytest

from app.candles.config import CandleConfig
from app.candles.engine import CandlestickEngine
from app.candles.geometry import compute_geometry
from tests.unit.candles.helpers import analyze, bars, trend_prefix

CFG = CandleConfig()


def test_basic_geometry() -> None:
    g = compute_geometry(bars([(100, 110, 95, 105)]), CFG).iloc[0]
    assert g["range"] == 15
    assert g["body"] == 5 and g["abs_body"] == 5
    assert g["upper_wick"] == 5 and g["lower_wick"] == 5
    assert g["body_ratio"] == pytest.approx(5 / 15)
    assert g["upper_wick_ratio"] == pytest.approx(5 / 15)
    assert g["lower_wick_ratio"] == pytest.approx(5 / 15)
    assert g["close_position"] == pytest.approx(10 / 15)
    assert g["direction"] == "bullish"
    assert math.isnan(g["gap"])  # no previous close


def test_ratios_sum_to_one() -> None:
    g = compute_geometry(bars([(101.3, 104.9, 99.7, 100.2), (100, 101, 98, 98.5)]), CFG)
    total = g["body_ratio"] + g["upper_wick_ratio"] + g["lower_wick_ratio"]
    assert np.allclose(total, 1.0)


def test_direction_labels() -> None:
    g = compute_geometry(
        bars([(100, 101, 99, 100.5), (100, 101, 99, 99.5), (100, 101, 99, 100)]), CFG
    )
    assert g["direction"].tolist() == ["bullish", "bearish", "neutral"]


def test_gap_uses_previous_close() -> None:
    g = compute_geometry(bars([(100, 101, 99, 100), (102, 103, 101, 102.5)]), CFG)
    assert g["gap"].iloc[1] == pytest.approx(0.02)


def test_zero_range_candle_has_undefined_ratios() -> None:
    g = compute_geometry(bars([(100, 100, 100, 100)]), CFG).iloc[0]
    assert g["valid"] and g["zero_range"] and not g["usable"]
    assert g["range"] == 0 and g["abs_body"] == 0
    for col in ("body_ratio", "upper_wick_ratio", "lower_wick_ratio", "close_position"):
        assert math.isnan(g[col])
    assert g["direction"] == "neutral"


def test_zero_range_candle_produces_no_patterns() -> None:
    a = analyze([(100, 101, 99, 100.5), (100, 100, 100, 100), (99.5, 102, 99, 101)])
    assert a.observations[a.observations["ts"] == a.geometry.index[1]].empty
    # Multi-bar patterns that would include the zero-range bar are not evaluated.
    after = a.observations[a.observations["ts"] == a.geometry.index[2]]
    assert (after["bars_in_pattern"] == 1).all()


@pytest.mark.parametrize(
    "candle",
    [
        (float("nan"), 101, 99, 100),
        (100, 101, 99, float("nan")),
        (100, 99, 98, 100.5),  # high below close
        (100, 101, 100.5, 100.2),  # low above open
        (0, 1, 0, 0.5),  # non-positive low
    ],
)
def test_invalid_rows_get_nan_geometry_and_no_patterns(candle: tuple[float, ...]) -> None:
    a = analyze([(100, 101, 99, 100.0), candle])  # type: ignore[list-item]
    row = a.geometry.iloc[1]
    assert not row["valid"]
    assert row["direction"] == "invalid"
    assert math.isnan(row["body_ratio"])
    assert not (a.observations["ts"] == a.geometry.index[1]).any()


def test_negative_volume_is_invalid() -> None:
    g = compute_geometry(bars([(100, 101, 99, 100.5)], volumes=[-1]), CFG)
    assert not g["valid"].iloc[0]


def test_relative_volume_requires_full_prior_window() -> None:
    n = CFG.relative_volume_lookback
    candles = [(100, 101, 99, 100.5)] * (n + 1)
    vols = [1000.0] * n + [3000.0]
    g = compute_geometry(bars(candles, vols), CFG)
    assert g["relative_volume"].iloc[:n].isna().all()  # fewer than n prior bars
    assert g["relative_volume"].iloc[n] == pytest.approx(3.0)  # uses PRIOR bars only


def test_relative_volume_window_with_missing_value_is_nan() -> None:
    n = CFG.relative_volume_lookback
    candles = [(100, 101, 99, 100.5)] * (n + 5)
    vols = [1000.0] * (n + 5)
    vols[3] = float("nan")
    g = compute_geometry(bars(candles, vols), CFG)
    assert math.isnan(g["relative_volume"].iloc[n])  # window rows 0..n-1 contains row 3
    assert math.isnan(g["relative_volume"].iloc[n + 3])  # window rows 3..n+2
    assert g["relative_volume"].iloc[n + 4] == pytest.approx(1.0)  # rows 4..n+3: complete


def test_trend_score_uses_only_prior_bars() -> None:
    prefix = trend_prefix("down", end_close=100)
    g = compute_geometry(bars([*prefix, (100, 130, 99, 129)]), CFG)  # huge last candle
    last = g.iloc[-1]
    # (close[t-1] - close[t-11]) / mean(range of t-10..t-1) = (100 - 110) / 2
    assert last["trend_score"] == pytest.approx(-5.0)
    assert last["prior_trend"] == "down"


def test_trend_unknown_without_history() -> None:
    g = compute_geometry(bars(trend_prefix("down", 100, n=5)), CFG)
    assert set(g["prior_trend"]) == {"unknown"}


def test_input_must_be_utc_sorted_unique_and_closed() -> None:
    engine = CandlestickEngine()
    b = bars([(100, 101, 99, 100.5), (100, 101, 99, 99.5)])
    with pytest.raises(ValueError, match="UTC"):
        engine.analyze(b.tz_convert("America/New_York"))
    with pytest.raises(ValueError, match="timezone-aware"):
        engine.analyze(b.tz_localize(None))
    with pytest.raises(ValueError, match="strictly increasing"):
        engine.analyze(b.iloc[::-1])
    with pytest.raises(ValueError, match="strictly increasing"):
        engine.analyze(pd.concat([b, b.iloc[[1]]]))
    with pytest.raises(ValueError, match="closed"):
        engine.analyze(b.assign(is_closed=[True, False]))
    with pytest.raises(ValueError, match="missing columns"):
        engine.analyze(b.drop(columns=["volume"]))
