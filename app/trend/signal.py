"""SMA200 state per asset (pure; design v2 section 6.2).

Computed on the asset's own sessions from its EUR total-return close. Rolling windows only look
backwards, so the state at session ``t`` depends on closes up to ``t`` and nothing later.
"""

import numpy as np
import pandas as pd

SMA_SESSIONS = 200
LONG = "LONG"
CASH = "CASH"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


def sma_states(close: pd.Series, window: int = SMA_SESSIONS) -> pd.Series:
    """State at every session: LONG if close > SMA (strictly), CASH if close <= SMA,
    INSUFFICIENT_DATA while fewer than `window` closes exist. The SMA includes the close."""
    values = close.to_numpy(dtype="float64")
    sma = close.rolling(window, min_periods=window).mean().to_numpy()
    states = np.where(values > sma, LONG, CASH).astype(object)
    states[: window - 1] = INSUFFICIENT_DATA
    states[np.isnan(sma)] = INSUFFICIENT_DATA
    return pd.Series(states, index=close.index, name="state", dtype=object)
