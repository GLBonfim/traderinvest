"""Exact hand-computed examples, edge cases, and an independent textbook-loop reference."""

import math
from itertools import pairwise

import numpy as np
import pandas as pd
import pytest

from app.indicators import indicators as ind


def s(values: list[float]) -> pd.Series:
    return pd.Series(values, dtype="float64")


def nan_eq(got: pd.Series, expected: list[float], atol: float = 1e-12) -> None:
    got_v, exp_v = got.to_numpy(dtype=float), np.array(expected, dtype=float)
    np.testing.assert_allclose(got_v, exp_v, rtol=0, atol=atol)


# ── independent reference: plain Python loops written from the textbook definitions ──


def ref_recursive(x: list[float], n: int, alpha: float) -> list[float]:
    out = [math.nan] * len(x)
    start = next(i for i, v in enumerate(x) if not math.isnan(v))
    if start + n - 1 >= len(x):
        return out
    prev = sum(x[start : start + n]) / n
    out[start + n - 1] = prev
    for t in range(start + n, len(x)):
        prev = alpha * x[t] + (1 - alpha) * prev
        out[t] = prev
    return out


def ref_rsi(close: list[float], n: int) -> list[float]:
    gains = [math.nan] + [max(b - a, 0.0) for a, b in pairwise(close)]
    losses = [math.nan] + [max(a - b, 0.0) for a, b in pairwise(close)]
    g, lo = ref_recursive(gains, n, 1 / n), ref_recursive(losses, n, 1 / n)
    out = []
    for gi, li in zip(g, lo, strict=True):
        if math.isnan(gi) or gi + li == 0:
            out.append(math.nan)
        else:
            out.append(100 - 100 / (1 + gi / li) if li else 100.0)
    return out


# ── SMA / EMA / Wilder ──


def test_sma_exact_and_warmup() -> None:
    nan_eq(ind.sma(s([1, 2, 3, 4, 5]), 3), [np.nan, np.nan, 2, 3, 4])


def test_ema_hand_example() -> None:
    # alpha = 0.5, seed = mean(2, 4, 6) = 4, then 0.5*8 + 0.5*4 = 6, 0.5*12 + 0.5*6 = 9
    nan_eq(ind.ema(s([2, 4, 6, 8, 12]), 3), [np.nan, np.nan, 4, 6, 9])


def test_wilder_hand_example() -> None:
    # alpha = 1/2, seed = mean(1, 2) = 1.5, then 2.25, 3.125, 4.0625
    nan_eq(ind.wilder(s([1, 2, 3, 4, 5]), 2), [np.nan, 1.5, 2.25, 3.125, 4.0625])


def test_recursive_skips_leading_nans_and_refuses_gaps() -> None:
    nan_eq(ind.ema(s([np.nan, np.nan, 2, 4, 6, 8]), 3), [np.nan] * 4 + [4, 6])
    with pytest.raises(ValueError, match="gap"):
        ind.ema(s([1, 2, np.nan, 4, 5]), 2)


def test_recursive_insufficient_history_is_all_nan() -> None:
    assert ind.ema(s([1, 2]), 3).isna().all()
    assert ind.ema(s([np.nan, np.nan]), 2).isna().all()


@pytest.mark.parametrize("n", [1, 2, 5, 13])
def test_ema_and_wilder_match_independent_loop(n: int) -> None:
    x = np.random.default_rng(n).normal(100, 5, 120).tolist()
    nan_eq(ind.ema(s(x), n), ref_recursive(x, n, 2 / (n + 1)))
    nan_eq(ind.wilder(s(x), n), ref_recursive(x, n, 1 / n))


# ── MACD ──


def test_macd_components_and_warmup() -> None:
    x = s(np.linspace(100, 130, 60).tolist())
    m = ind.macd(x, 3, 6, 4)
    assert m["macd"].first_valid_index() == 5  # slow - 1
    assert m["macd_signal"].first_valid_index() == 8  # (slow - 1) + (signal - 1)
    expected_line = ind.ema(x, 3) - ind.ema(x, 6)
    pd.testing.assert_series_equal(m["macd"], expected_line, check_names=False)
    pd.testing.assert_series_equal(m["macd_hist"], m["macd"] - m["macd_signal"], check_names=False)


# ── RSI ──


def test_rsi_hand_example() -> None:
    # gains [1, 1, 0, 2], losses [0, 0, 1, 0]; n=2 seeds at bar 2: 1 / 0 -> 100;
    # bar 3: 0.5 / 0.5 -> 50; bar 4: 1.25 / 0.25 -> RS 5 -> 83.333...
    nan_eq(ind.rsi(s([10, 11, 12, 11, 13]), 2), [np.nan, np.nan, 100, 50, 100 * 5 / 6], atol=1e-10)


def test_rsi_edge_cases() -> None:
    assert ind.rsi(s([5.0] * 10), 3).isna().all()  # no gains and no losses: undefined
    falling = ind.rsi(s([10, 9, 8, 7, 6, 5]), 3)
    assert (falling.dropna() == 0).all()  # only losses: 0 (well-defined limit)
    rising = ind.rsi(s([1, 2, 3, 4, 5, 6]), 3)
    assert (rising.dropna() == 100).all()


def test_rsi_matches_independent_loop() -> None:
    x = (100 + np.cumsum(np.random.default_rng(3).normal(0, 1, 200))).tolist()
    nan_eq(ind.rsi(s(x), 14), ref_rsi(x, 14), atol=1e-10)  # RSI is rounded to 1e-10


# ── Stochastic ──


def test_stochastic_hand_example() -> None:
    high, low = s([10, 12, 11, 13]), s([8, 9, 9, 10])
    close = s([9, 11, 10, 12])
    st = ind.stochastic(high, low, close, n=3, k_smooth=1, d_period=2)
    # bar 2: HH 12, LL 8 -> 100 (10-8)/4 = 50; bar 3: HH 13, LL 9 -> 100 (12-9)/4 = 75
    nan_eq(st["stoch_raw_k"], [np.nan, np.nan, 50, 75])
    nan_eq(st["stoch_d"], [np.nan, np.nan, np.nan, 62.5])


def test_stochastic_flat_window_is_undefined_and_propagates() -> None:
    flat = s([10.0] * 6)
    st = ind.stochastic(flat, flat, flat, n=3, k_smooth=2, d_period=2)
    assert st.isna().all().all()


# ── ROC / TR / ATR ──


def test_roc_exact() -> None:
    nan_eq(ind.roc(s([100, 110, 99]), 1), [np.nan, 10, -10])


def test_true_range_cases() -> None:
    high, low, close = s([10, 110, 50]), s([9, 105, 48]), s([9.5, 108, 49])
    # bar 1: gap up: |110 - 9.5| = 100.5; bar 2: gap down: |48 - 108| = 60
    nan_eq(ind.true_range(high, low, close), [np.nan, 100.5, 60])


def test_atr_warmup_and_value() -> None:
    high, low, close = s([10, 11, 12, 13]), s([9, 10, 11, 12]), s([9.5, 10.5, 11.5, 12.5])
    atr = ind.wilder(ind.true_range(high, low, close), 2)
    # TR from bar 1: 1.5, 1.5, 1.5 -> ATR(2) first at bar 2
    nan_eq(atr, [np.nan, np.nan, 1.5, 1.5])


# ── Bollinger / realized volatility ──


def test_bollinger_population_std() -> None:
    bb = ind.bollinger(s([1, 2, 3, 4]), 4, 2.0, ddof=0)
    std = math.sqrt(1.25)
    assert bb["bb_middle"].iloc[-1] == 2.5
    assert bb["bb_upper"].iloc[-1] == pytest.approx(2.5 + 2 * std)
    assert bb["bb_percent_b"].iloc[-1] == pytest.approx((4 - (2.5 - 2 * std)) / (4 * std))
    assert bb["bb_middle"].iloc[:3].isna().all()
    sample = ind.bollinger(s([1, 2, 3, 4]), 4, 2.0, ddof=1)
    assert sample["bb_upper"].iloc[-1] == pytest.approx(2.5 + 2 * math.sqrt(5 / 3))


def test_bollinger_flat_prices() -> None:
    bb = ind.bollinger(s([7.0] * 5), 5, 2.0, ddof=0)
    last = bb.iloc[-1]
    assert last["bb_upper"] == last["bb_lower"] == 7.0
    assert last["bb_width_pct"] == 0.0  # zero width is well-defined
    assert math.isnan(last["bb_percent_b"])  # position within a zero-width band is not


def test_realized_volatility_exact() -> None:
    close = s([100, 110, 99, 108.9])
    r = np.log([1.1, 0.9, 1.1])
    expected = float(np.std(r, ddof=1) * math.sqrt(252))
    rv = ind.realized_volatility(close, 3, ddof=1, periods_per_year=252)
    assert rv.iloc[:3].isna().all()  # needs 3 returns -> first value at bar 3
    assert rv.iloc[3] == pytest.approx(expected)


def test_constant_growth_has_zero_volatility() -> None:
    rv = ind.realized_volatility(s([100 * 1.01**i for i in range(10)]), 5, 1, 252)
    assert rv.dropna().abs().max() < 1e-12


# ── volume ──


def test_relative_volume_excludes_current_bar() -> None:
    rv = ind.relative_volume(s([100, 100, 100, 400]), 3)
    nan_eq(rv, [np.nan, np.nan, np.nan, 4.0])


def test_relative_volume_zero_baseline_and_missing() -> None:
    assert math.isnan(ind.relative_volume(s([0, 0, 0, 50]), 3).iloc[-1])  # 50 / 0 undefined
    assert ind.relative_volume(s([0, 0, 0, 0]), 3).isna().all()
    with_gap = ind.relative_volume(s([100, np.nan, 100, 100, 100, 100]), 2)
    nan_eq(with_gap, [np.nan, np.nan, np.nan, np.nan, 1.0, 1.0])


def test_obv_rules() -> None:
    nan_eq(ind.obv(s([10, 11, 11, 10]), s([100, 200, 300, 400])), [0, 200, 200, -200])


def test_obv_missing_volume_propagates() -> None:
    out = ind.obv(s([10, 11, 12, 13]), s([100, 200, np.nan, 400]))
    nan_eq(out, [0, 200, np.nan, np.nan])
