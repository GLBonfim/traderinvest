"""Candle geometry. Vectorised; every value at row t uses only rows <= t.

Definitions (R = high - low):
    body              = close - open                (signed)
    abs_body          = |close - open|
    upper_wick        = high - max(open, close)
    lower_wick        = min(open, close) - low
    body_ratio        = abs_body / R
    upper_wick_ratio  = upper_wick / R
    lower_wick_ratio  = lower_wick / R
    close_position    = (close - low) / R           (0 = closed at low, 1 = at high)
    gap               = open / previous close - 1
    relative_volume   = volume / mean(volume of the previous N bars)
    trend_score       = (close[t-1] - close[t-1-N]) / mean(R of bars t-N..t-1)

Ratios are rounded to RATIO_DECIMALS (1e-10, far below 6-decimal price precision) so that a
value mathematically equal to a threshold (e.g. 0.2 / 2 = 0.1) is not lost to binary
floating-point error (0.10000000000000142).

Zero-range candles (R == 0) are valid data but all ratios are undefined (NaN) and they never
qualify for a pattern. Invalid rows (missing values, high < max(open, close), low > min(...),
non-positive prices, negative volume) get NaN geometry and direction "invalid".
"""

import numpy as np
import pandas as pd

from app.candles.config import CandleConfig

OHLCV = ("open", "high", "low", "close", "volume")
RATIO_DECIMALS = 10


def ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """numerator / denominator, NaN where the denominator is not > 0, rounded (see module doc)."""
    return (numerator / denominator.where(denominator > 0)).round(RATIO_DECIMALS)


def _prior_mean(series: pd.Series, lookback: int) -> pd.Series:
    """Mean of the `lookback` bars strictly BEFORE each row; NaN without full history."""
    return series.shift(1).rolling(lookback, min_periods=lookback).mean()


def compute_geometry(bars: pd.DataFrame, cfg: CandleConfig) -> pd.DataFrame:
    o, h, lo, c, v = (bars[col].astype("float64") for col in OHLCV)

    finite = bars[list(OHLCV)].notna().all(axis=1)
    body_top = pd.concat([o, c], axis=1).max(axis=1, skipna=False)
    body_bottom = pd.concat([o, c], axis=1).min(axis=1, skipna=False)
    valid = finite & (h >= body_top) & (lo <= body_bottom) & (lo > 0) & (v >= 0)

    rng = (h - lo).where(valid)
    zero_range = valid & (rng == 0)
    usable = valid & ~zero_range
    denom = rng.where(usable)  # NaN where ratios are undefined

    body = (c - o).where(valid)
    abs_body = body.abs()
    upper_wick = (h - body_top).where(valid)
    lower_wick = (body_bottom - lo).where(valid)

    direction = pd.Series(
        np.select([body > 0, body < 0, valid], ["bullish", "bearish", "neutral"], "invalid"),
        index=bars.index,
    )

    close_v = c.where(valid)
    prev_close = close_v.shift(1)

    vol_mean = _prior_mean(v.where(valid), cfg.relative_volume_lookback)
    rel_volume = ratio(v.where(valid), vol_mean)

    n = cfg.trend_lookback
    trend_range = _prior_mean(rng, n)
    trend_score = ratio(close_v.shift(1) - close_v.shift(n + 1), trend_range)
    prior_trend = pd.Series(
        np.select(
            [
                trend_score >= cfg.trend_min_score,
                trend_score <= -cfg.trend_min_score,
                trend_score.notna(),
            ],
            ["up", "down", "none"],
            "unknown",
        ),
        index=bars.index,
    )

    return pd.DataFrame(
        {
            "open": o,
            "high": h,
            "low": lo,
            "close": c,
            "volume": v,
            "valid": valid,
            "zero_range": zero_range,
            "usable": usable,
            "range": rng,
            "body": body,
            "abs_body": abs_body,
            "upper_wick": upper_wick,
            "lower_wick": lower_wick,
            "body_ratio": ratio(abs_body, denom),
            "upper_wick_ratio": ratio(upper_wick, denom),
            "lower_wick_ratio": ratio(lower_wick, denom),
            "close_position": ratio(close_v - lo, denom),
            "direction": direction,
            "gap": (o.where(valid) / prev_close - 1).round(RATIO_DECIMALS),
            "relative_volume": rel_volume,
            "avg_body_prior": _prior_mean(abs_body, cfg.long_body_lookback),
            "trend_score": trend_score,
            "prior_trend": prior_trend,
        },
        index=bars.index,
    )
