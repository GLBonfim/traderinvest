import numpy as np
import pandas as pd
import pytest

from app.regimes import regimes as rg
from app.regimes.config import RegimeConfig

CFG = RegimeConfig()
NAN = float("nan")


def s(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


# ── causal percentile ──


def test_percentile_excludes_current_and_needs_min_observations() -> None:
    p = rg.causal_percentile_rank(s([1, 2, 3, 4, 5]), lookback=3, min_observations=2)
    assert p["reference_count"].tolist() == [0, 1, 2, 3, 3]
    np.testing.assert_array_equal(p["percentile"].to_numpy(), [NAN, NAN, 1.0, 1.0, 1.0])


def test_percentile_mid_rank_ties() -> None:
    # reference [1, 2, 3, 4], x = 2: (1 below + 0.5 * 1 equal) / 4 = 0.375
    p = rg.causal_percentile_rank(s([1, 2, 3, 4, 2]), lookback=4, min_observations=4)
    assert p["percentile"].iloc[-1] == 0.375
    flat = rg.causal_percentile_rank(s([7.0] * 6), lookback=5, min_observations=3)
    assert (flat["percentile"].dropna() == 0.5).all()


def test_percentile_never_sees_the_current_or_future_values() -> None:
    # A spike at t is ranked only against earlier values; the spike never enters t's reference.
    p = rg.causal_percentile_rank(s([1, 1, 1, 9, 1]), lookback=10, min_observations=3)
    assert p["percentile"].iloc[3] == 1.0
    # t=4: x=1 vs PAST reference [1, 1, 1, 9]: (0 below + 0.5 * 3 equal) / 4
    assert p["percentile"].iloc[4] == 0.375


def test_percentile_lookback_boundary() -> None:
    # lookback 2: reference for the last value is [3, 4] only (1 and 2 are too old)
    p = rg.causal_percentile_rank(s([1, 2, 3, 4, 2.5]), lookback=2, min_observations=2)
    assert p["percentile"].iloc[-1] == 0.0


def test_percentile_nan_handling() -> None:
    p = rg.causal_percentile_rank(s([1, NAN, 2, 3, NAN, 4]), lookback=10, min_observations=2)
    assert p["reference_count"].tolist() == [0, 1, 1, 2, 3, 3]  # NaNs are not references
    assert np.isnan(p["percentile"].iloc[4])  # current NaN -> NaN
    assert p["percentile"].iloc[3] == 1.0 and p["percentile"].iloc[5] == 1.0


# ── thresholds ──


@pytest.mark.parametrize(
    ("pct", "expected"),
    [
        (NAN, "insufficient_data"),
        (0.0, "low"),
        (0.1999999999, "low"),
        (0.20, "normal"),
        (0.7999999999, "normal"),
        (0.80, "high"),
        (0.9499999999, "high"),
        (0.95, "extreme"),
        (1.0, "extreme"),
    ],
)
def test_volatility_thresholds(pct: float, expected: str) -> None:
    assert rg.volatility_regime(s([pct]), CFG).iloc[0] == expected


@pytest.mark.parametrize(
    ("rsi", "roc", "macd", "expected"),
    [
        (60, 1, 1, "positive"),
        (40, -1, -1, "negative"),
        (70, 1, 1, "extreme_positive"),  # boundary inclusive
        (69.99, 1, 1, "positive"),
        (30, -1, -1, "extreme_negative"),
        (30.01, -1, -1, "negative"),
        (50, 1, 1, "neutral"),  # RSI exactly at the midpoint
        (60, 0, 1, "neutral"),  # ROC exactly zero
        (60, 1, -1, "neutral"),  # disagreement
        (80, -1, 1, "neutral"),  # extreme RSI without agreement is not extreme momentum
        (20, 1, -1, "neutral"),
        (NAN, 1, 1, "insufficient_data"),
        (60, NAN, 1, "insufficient_data"),
        (60, 1, NAN, "insufficient_data"),
    ],
)
def test_momentum_rules(rsi: float, roc: float, macd: float, expected: str) -> None:
    assert rg.momentum_regime(s([rsi]), s([roc]), s([macd]), CFG).iloc[0] == expected


@pytest.mark.parametrize(
    ("rv", "expected"),
    [(NAN, "insufficient_data"), (0.0, "low"), (0.4999, "low"), (0.5, "normal"),
     (1.4999, "normal"), (1.5, "high"), (10.0, "high")],
)  # fmt: skip
def test_participation_thresholds(rv: float, expected: str) -> None:
    assert rg.participation_state(s([rv]), CFG).iloc[0] == expected


# ── composite ──

TRENDS = ["uptrend", "downtrend", "range", "transition", "insufficient_data"]
VOLS = ["low", "normal", "high", "extreme", "insufficient_data"]
EXPECTED_COMPOSITE = (
    {("uptrend", v): "trending_up" for v in VOLS[:4]}
    | {("downtrend", v): "trending_down" for v in VOLS[:4]}
    | {("range", v): "ranging" for v in VOLS[:4]}
    | {
        ("transition", "low"): "low_volatility_transition",
        ("transition", "normal"): "transition",
        ("transition", "high"): "high_volatility_transition",
        ("transition", "extreme"): "high_volatility_transition",
    }
)


@pytest.mark.parametrize("trend", TRENDS)
@pytest.mark.parametrize("vol", VOLS)
def test_composite_mapping_is_total_and_explicit(trend: str, vol: str) -> None:
    got = rg.composite_regime(pd.Series([trend]), pd.Series([vol])).iloc[0]
    assert got == EXPECTED_COMPOSITE.get((trend, vol), "insufficient_data")


def test_composite_rejects_unknown_trend() -> None:
    with pytest.raises(ValueError):
        rg.composite_regime(pd.Series(["sideways"]), pd.Series(["normal"]))


# ── transitions ──


def test_transitions_and_age() -> None:
    t = rg.transitions(pd.Series(list("aabbba")))
    assert t["changed"].tolist() == [False, False, True, False, False, True]
    assert t["age"].tolist() == [1, 2, 1, 2, 3, 1]


def test_long_period_without_transition() -> None:
    t = rg.transitions(pd.Series(["normal"] * 500))
    assert not t["changed"].any()
    assert t["age"].tolist() == list(range(1, 501))
