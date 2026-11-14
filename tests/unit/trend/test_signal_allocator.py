import numpy as np
import pandas as pd
import pytest

from app.trend.allocator import (
    needs_trade,
    realised_vol,
    target_weight,
    vol_is_usable,
    whole_units,
)
from app.trend.signal import CASH, INSUFFICIENT_DATA, LONG, sma_states


def _series(values: list[float] | np.ndarray) -> pd.Series:
    return pd.Series(values, index=pd.bdate_range("2020-01-01", periods=len(values)))


def test_sma_needs_200_closes() -> None:
    states = sma_states(_series(np.linspace(1.0, 2.0, 201)))
    assert states.iloc[198] == INSUFFICIENT_DATA  # 199 closes
    assert states.iloc[199] == LONG  # 200th close, rising series is above its mean
    assert (states.iloc[:199] == INSUFFICIENT_DATA).all()


def test_close_equal_to_sma_is_cash() -> None:
    states = sma_states(_series([5.0] * 210))
    assert (states.iloc[199:] == CASH).all()


def test_close_below_sma_is_cash() -> None:
    states = sma_states(_series(np.linspace(2.0, 1.0, 205)))
    assert (states.iloc[199:] == CASH).all()


def test_state_only_depends_on_past_closes() -> None:
    values = np.random.default_rng(1).lognormal(0, 0.01, 400).cumprod()
    full = sma_states(_series(values))
    cut = sma_states(_series(values[:300]))
    assert full.iloc[:300].tolist() == cut.tolist()


def test_volatility_cap_and_weight() -> None:
    assert target_weight(LONG, 0.05, 10) == pytest.approx(0.1)  # cap at 1/N
    assert target_weight(LONG, 0.20, 10) == pytest.approx(0.05)  # 10 % / 20 % of 1/N
    assert target_weight(CASH, 0.20, 10) == 0.0
    assert target_weight(INSUFFICIENT_DATA, 0.20, 10) == 0.0
    weights = [target_weight(LONG, v, 11) for v in (0.01, 0.05, 0.1, 0.3, 0.9)] * 2 + [0.1]
    assert sum(weights[:11]) <= 1.0
    with pytest.raises(ValueError):
        target_weight(LONG, float("nan"), 10)


def test_realised_vol_matches_definition_and_flags_stale_windows() -> None:
    rng = np.random.default_rng(2)
    values = 100 * np.cumprod(1 + rng.normal(0, 0.01, 120))
    table = realised_vol(_series(values))
    r = pd.Series(values).pct_change()
    expected = r.iloc[-60:].std(ddof=1) * np.sqrt(252)
    assert table["vol"].iloc[-1] == pytest.approx(expected)
    assert np.isnan(table["vol"].iloc[59]) and np.isfinite(table["vol"].iloc[60])
    assert not table["stale"].any()
    flat = values.copy()
    flat[70:77] = flat[69]  # six consecutive zero returns
    stale = realised_vol(_series(flat))
    assert stale["stale"].iloc[80]
    assert not vol_is_usable(float(stale["vol"].iloc[80]), bool(stale["stale"].iloc[80]))
    five = values.copy()
    five[70:75] = five[69]  # five zero returns: allowed
    assert not realised_vol(_series(five))["stale"].any()


def test_drift_threshold_is_strict() -> None:
    assert not needs_trade(LONG, LONG, w_actual=0.625, w_target=0.5)  # exactly 25 %
    assert not needs_trade(LONG, LONG, w_actual=0.375, w_target=0.5)
    assert needs_trade(LONG, LONG, w_actual=0.6251, w_target=0.5)
    assert needs_trade(LONG, None, w_actual=0.0, w_target=0.05)  # first entry
    assert needs_trade(CASH, LONG, w_actual=0.1, w_target=0.0)  # exit
    assert needs_trade(LONG, CASH, w_actual=0.0, w_target=0.05)
    assert not needs_trade(CASH, CASH, w_actual=0.0, w_target=0.0)
    assert not needs_trade(INSUFFICIENT_DATA, LONG, w_actual=0.1, w_target=0.0)


def test_whole_units_round_toward_zero() -> None:
    assert whole_units(0.5, 1000.0, 33.0) == 15
    assert whole_units(0.0, 1000.0, 33.0) == 0
    assert whole_units(0.001, 1000.0, 33.0) == 0
    with pytest.raises(ValueError):
        whole_units(0.5, 1000.0, 0.0)
