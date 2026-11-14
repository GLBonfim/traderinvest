"""Volatility, target weights and the trading rule (pure; design v2 sections 6.3 and 6.4)."""

import math

import numpy as np
import pandas as pd

from app.trend.signal import CASH, LONG

VOL_SESSIONS = 60
VOL_TARGET = 0.10
ANNUALISATION = 252
DRIFT_THRESHOLD = 0.25
MAX_ZERO_RUN = 5  # more consecutive zero returns inside the window = stale data


def realised_vol(close: pd.Series, window: int = VOL_SESSIONS) -> pd.DataFrame:
    """Per session: annualised stdev (ddof=1) of the last `window` simple returns and a flag for
    a stale window (more than MAX_ZERO_RUN consecutive zero returns inside it)."""
    returns = close.pct_change()
    vol = returns.rolling(window, min_periods=window).std(ddof=1) * math.sqrt(ANNUALISATION)
    zero = returns.eq(0.0).to_numpy()
    run = np.zeros(len(zero), dtype=np.int64)
    for i in range(len(zero)):
        run[i] = run[i - 1] + 1 if zero[i] and i > 0 else int(zero[i])
    longest = pd.Series(run, index=close.index).rolling(window, min_periods=1).max()
    stale = (longest > MAX_ZERO_RUN).to_numpy()
    return pd.DataFrame({"vol": vol.to_numpy(), "stale": stale}, index=close.index)


def vol_is_usable(vol: float, stale: bool) -> bool:
    return bool(np.isfinite(vol)) and vol > 0 and not stale


def target_weight(state: str, vol: float, n_assets: int, vol_target: float = VOL_TARGET) -> float:
    """``(1/N) * min(1, vol_target / vol)`` when LONG, else 0. `vol` must be usable if LONG."""
    if state != LONG:
        return 0.0
    if not np.isfinite(vol) or vol <= 0:
        raise ValueError("LONG target needs a positive finite volatility")
    return (1.0 / n_assets) * min(1.0, vol_target / vol)


def needs_trade(
    state: str,
    last_state: str | None,
    w_actual: float,
    w_target: float,
    threshold: float = DRIFT_THRESHOLD,
) -> bool:
    """Trading rule: a state change LONG <-> CASH, or (LONG) relative drift strictly above the
    threshold. INSUFFICIENT_DATA never trades."""
    if state not in (LONG, CASH):
        return False
    if last_state in (LONG, CASH) and state != last_state:
        return True
    if state == LONG:
        return abs(w_actual - w_target) / w_target > threshold
    return False


def whole_units(weight: float, nav: float, price: float) -> int:
    """Target quantity from decision-time prices: whole units, rounded toward zero."""
    if price <= 0:
        raise ValueError("price must be positive")
    return math.trunc(weight * nav / price)
