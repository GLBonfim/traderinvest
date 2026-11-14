"""Causal CRT/MSS/OTE SELL filter, frozen research convention for a NEW M5 strategy."""

import numpy as np
import pandas as pd

VERSION = "aula02-crt-m5/1.0.0"


def allowed(mid: pd.DataFrame) -> pd.Series:
    """UTC H4 previous complete range, sweep, later 3-bar break, later OTE rejection.
    Sequence expires 12 M5 bars after sweep. No anchor redrawing, no future pivots.
    This local adaptation is unapproved until the NEW full pipeline passes its gates.
    """
    idx = pd.DatetimeIndex(mid.index).tz_convert("UTC")
    f = mid.copy()
    f.index = idx
    h4 = f.resample("4h").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    complete = f.close.resample("4h").count() == 48
    h4 = h4.loc[complete].shift(0)
    high, low, op, close = (f[c].to_numpy(float) for c in ("high", "low", "open", "close"))
    refs = idx.floor("4h") - pd.Timedelta(hours=4)
    reference = h4.reindex(refs)
    rh, rl = reference.high.to_numpy(float), reference.low.to_numpy(float)
    bearish = (reference.close < reference.open).to_numpy(bool)
    mask = np.zeros(len(f), dtype=bool)
    stage = 0
    origin = base = level = 0.0
    sweep_i = -100
    for i in range(3, len(f)):
        if i > 3 and idx[i] - idx[i - 1] != pd.Timedelta(minutes=5):
            stage = 0
        if stage and (i - sweep_i > 12 or high[i] >= origin):
            stage = 0
        if stage == 2:
            zl = base + 0.62 * (origin - base)
            zh = base + 0.79 * (origin - base)
            if low[i] < base:
                stage = 0
            elif high[i] >= zl:
                mask[i] = bool(
                    high[i] <= zh and close[i] < zl and close[i] < op[i] and close[i] > rl[sweep_i]
                )
                stage = 0  # first retest only, never cherry-pick a later reaction
        elif stage == 1:
            base = min(base, low[i])
            if close[i] < level and close[i] < op[i]:
                stage = 2
        if (
            stage == 0
            and bearish[i]
            and high[i] > rh[i]
            and rl[i] < close[i] < rh[i]
            and low[i] >= rl[i]
        ):
            stage = 1
            origin = high[i]
            base = low[i]
            level = float(low[i - 3 : i].min())
            sweep_i = i
    return pd.Series(mask, index=mid.index)
