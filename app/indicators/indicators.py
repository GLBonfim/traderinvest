"""Transparent indicator formulas (pandas/numpy). Every value at row t uses rows <= t only.

Conventions (docs/technical-indicators.md):
- Rolling windows are TRAILING and require a FULL window (`min_periods = n`); no centred windows.
- Recursive smoothers (EMA, Wilder) are seeded with the simple mean of their first n valid
  inputs; the first output is at that n-th input. Leading NaNs (warm-up of an upstream series)
  are skipped; a NaN AFTER the first valid input is not allowed (raises), because a recursive
  state cannot be continued honestly across a gap.
- Undefined values (0/0, flat windows) are NaN, never an arbitrary number. Mathematically
  well-defined limits are kept (e.g. RSI = 100 when there are gains and no losses).
- Results are rounded to 1e-10 where a ratio is formed, matching the other engines.
"""

import numpy as np
import pandas as pd

ROUND = 10


def _ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    """num / den; NaN where den == 0 or either side is NaN."""
    return (num / den.where(den != 0)).round(ROUND)


def sma(x: pd.Series, n: int) -> pd.Series:
    """Mean of the last n values (trailing, full window)."""
    return x.rolling(n, min_periods=n).mean()


def recursive_smooth(x: pd.Series, n: int, alpha: float) -> pd.Series:
    """y[s] = mean(x[f .. f+n-1]) at s = f+n-1 (f = first valid input),
    then y[t] = alpha * x[t] + (1 - alpha) * y[t-1]."""
    values = x.to_numpy(dtype=np.float64)
    out = np.full(values.shape, np.nan)
    valid = np.flatnonzero(~np.isnan(values))
    if valid.size == 0:
        return pd.Series(out, index=x.index)
    first = int(valid[0])
    if np.isnan(values[first:]).any():
        raise ValueError("recursive smoothing input has a gap after its first valid value")
    seed = first + n - 1
    if seed >= len(values):
        return pd.Series(out, index=x.index)
    prev = float(values[first : seed + 1].mean())
    out[seed] = prev
    for t in range(seed + 1, len(values)):
        prev = alpha * values[t] + (1.0 - alpha) * prev
        out[t] = prev
    return pd.Series(out, index=x.index)


def ema(x: pd.Series, n: int) -> pd.Series:
    """Exponential moving average, alpha = 2 / (n + 1), SMA seed."""
    return recursive_smooth(x, n, 2.0 / (n + 1))


def wilder(x: pd.Series, n: int) -> pd.Series:
    """Wilder's smoothing (RMA), alpha = 1 / n, SMA seed."""
    return recursive_smooth(x, n, 1.0 / n)


def macd(close: pd.Series, fast: int, slow: int, signal: int) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def rsi(close: pd.Series, n: int) -> pd.Series:
    """Wilder RSI. avg_gain, avg_loss = Wilder(n) of gains/losses of close-to-close changes.
    RSI = 100 - 100 / (1 + avg_gain / avg_loss)
        = 100 if avg_loss == 0 < avg_gain;  0 if avg_gain == 0 < avg_loss;  NaN if both 0."""
    delta = close.diff()
    gain = wilder(delta.clip(lower=0), n)
    loss = wilder((-delta).clip(lower=0), n)
    total = gain + loss
    out = (100.0 * gain / total.where(total != 0)).round(ROUND)  # algebraically identical form
    return out


def stochastic(
    high: pd.Series, low: pd.Series, close: pd.Series, n: int, k_smooth: int, d_period: int
) -> pd.DataFrame:
    """raw %K = 100 (close - LL_n) / (HH_n - LL_n), NaN when HH_n == LL_n;
    %K = SMA(raw %K, k_smooth); %D = SMA(%K, d_period)."""
    hh = high.rolling(n, min_periods=n).max()
    ll = low.rolling(n, min_periods=n).min()
    raw = _ratio(100.0 * (close - ll), hh - ll)
    k = sma(raw, k_smooth)
    return pd.DataFrame({"stoch_raw_k": raw, "stoch_k": k, "stoch_d": sma(k, d_period)})


def roc(close: pd.Series, n: int) -> pd.Series:
    """100 (close / close[t-n] - 1)."""
    return (100.0 * (close / close.shift(n) - 1)).round(ROUND)


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """max(high - low, |high - prev close|, |low - prev close|); NaN on the first bar
    (no previous close)."""
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(
        axis=1, skipna=False
    )
    return tr.where(prev.notna())


def bollinger(close: pd.Series, n: int, k: float, ddof: int) -> pd.DataFrame:
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std(ddof=ddof)
    upper, lower = mid + k * std, mid - k * std
    return pd.DataFrame(
        {
            "bb_middle": mid,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_width_pct": _ratio(upper - lower, mid),
            "bb_percent_b": _ratio(close - lower, upper - lower),
        }
    )


def realized_volatility(close: pd.Series, n: int, ddof: int, periods_per_year: int) -> pd.Series:
    """std of the last n close-to-close log returns x sqrt(periods_per_year)."""
    log_ret = pd.Series(np.log(close / close.shift(1)), index=close.index)
    return log_ret.rolling(n, min_periods=n).std(ddof=ddof) * float(np.sqrt(periods_per_year))


def relative_volume(volume: pd.Series, n: int) -> pd.Series:
    """volume[t] / mean(volume[t-n .. t-1]) — the current bar is EXCLUDED from the baseline
    (same definition as the Candlestick Engine's relative_volume)."""
    baseline = volume.shift(1).rolling(n, min_periods=n).mean()
    return _ratio(volume, baseline)


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """OBV[0] = 0; OBV[t] = OBV[t-1] + sign(close[t] - close[t-1]) * volume[t]
    (unchanged close adds 0). A missing volume makes OBV undefined from that bar onwards."""
    diff = close.diff()
    direction = pd.Series(np.sign(diff), index=close.index).fillna(0.0)  # bar 0: no prior close
    step = direction * volume  # NaN wherever volume is missing
    return step.cumsum(skipna=False)  # NaN propagates to every later bar
