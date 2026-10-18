"""Vectorised, strictly causal signal logic of the v2 agents.

The SAME functions drive the research backtests (whole history at once) and the production
agents (the last row of their bar window), so what is validated is exactly what runs. Every
value at row t uses rows <= t only; entries are executed by the caller at the NEXT bar's open.

Senior-trader practices encoded here (fixed rules, few parameters):
- trade only liquid hours (London or New York session) and away from the 17:00 NY rollover;
- higher-timeframe context: EMA(200) on M15 (~ EMA(50) on H1) for the trend direction;
- Trend: join an established trend on a PULLBACK to EMA(20), not by chasing;
- Momentum: breakout of the previous N-bar range with expanding volatility, not exhausted;
- Mean reversion: only in a ranging market (low efficiency ratio), after a CONFIRMED rejection
  (close back inside the Bollinger band), targeting the mean;
- Regime: veto entries when volatility is extreme or dead, or the spread eats the move;
- every trade has an ATR-based stop, a target with risk/reward >= the risk manager's minimum and
  a time stop (an idea that does not work within its horizon is closed).
"""

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from app.indicators.indicators import bollinger, ema, rsi, true_range, wilder

ROLLOVER_GUARD_MIN = 15


@dataclass(frozen=True)
class TrendParams:
    fast: int = 20
    slow: int = 50
    htf: int = 200
    slope_bars: int = 20
    min_slope_atr: float = 0.5
    pullback_bars: int = 3
    stop_atr: float = 2.0
    target_atr: float = 3.0
    max_hold: int = 48


@dataclass(frozen=True)
class MomentumParams:
    channel: int = 20
    htf: int = 200
    atr_expansion: float = 1.0  # ATR / SMA(ATR, 50) must exceed this
    rsi_min: float = 55.0
    rsi_max: float = 80.0
    stop_atr: float = 1.5
    target_atr: float = 2.5
    max_hold: int = 24


@dataclass(frozen=True)
class MeanReversionParams:
    band_k: float = 2.0
    max_er: float = 0.25
    rsi_extreme: float = 35.0
    stop_buffer_atr: float = 0.5
    min_rr: float = 1.2
    max_hold: int = 16


@dataclass(frozen=True)
class RegimeParams:
    er_trend: float = 0.30
    er_range: float = 0.18
    atr_pct_high: float = 0.97
    atr_pct_low: float = 0.03
    max_spread_atr: float = 0.30


DEFAULT_TREND = TrendParams()
DEFAULT_MOMENTUM = MomentumParams()
DEFAULT_MEAN_REVERSION = MeanReversionParams()
DEFAULT_REGIME = RegimeParams()


def params_dict(p: object) -> dict[str, float]:
    return {k: float(v) for k, v in asdict(p).items()}  # type: ignore[call-overload]


def mid_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Mid OHLC from a frame with bid_/ask_ columns (or a mid frame passed through)."""
    if "open" in df.columns:
        return df[["open", "high", "low", "close"]].astype(float)
    return pd.DataFrame(
        {k: (df[f"bid_{k}"] + df[f"ask_{k}"]) / 2 for k in ("open", "high", "low", "close")},
        index=df.index,
    )


def session_flags(index: pd.DatetimeIndex) -> pd.DataFrame:
    """Liquid hours (London 08-17 local or New York 08-17 local) and the rollover guard."""
    idx = index.tz_convert("UTC") if index.tz is not None else index.tz_localize("UTC")
    lon = idx.tz_convert("Europe/London")
    ny = idx.tz_convert("America/New_York")
    lon_h = lon.hour + lon.minute / 60
    ny_h = ny.hour + ny.minute / 60
    liquid = ((lon_h >= 8) & (lon_h < 17)) | ((ny_h >= 8) & (ny_h < 17))
    guard = ROLLOVER_GUARD_MIN / 60
    near_rollover = (ny_h >= 17 - guard - 0.25) & (ny_h < 17 + guard)  # bar may end at 17:00
    return pd.DataFrame({"liquid": liquid, "near_rollover": near_rollover}, index=index)


def features(mid: pd.DataFrame, spread: pd.Series | None = None) -> pd.DataFrame:
    """Shared causal features on mid prices (and the bar's closing spread, if known)."""
    c, h, lo = mid["close"], mid["high"], mid["low"]
    f = pd.DataFrame(index=mid.index)
    f["close"], f["high"], f["low"] = c, h, lo
    f["atr"] = wilder(true_range(h, lo, c), 14)
    f["atr_sma"] = f["atr"].rolling(50, min_periods=50).mean()
    f["atr_pct"] = f["atr"].rolling(200, min_periods=100).rank(pct=True)
    net = (c - c.shift(20)).abs()
    path = c.diff().abs().rolling(20, min_periods=20).sum()
    f["er"] = net / path.where(path > 0)
    f["rsi"] = rsi(c, 14)
    f["spread"] = spread if spread is not None else np.nan
    f = f.join(session_flags(pd.DatetimeIndex(mid.index)))
    f["tradable"] = f["liquid"] & ~f["near_rollover"]
    return f


def _signal_frame(index: pd.Index) -> pd.DataFrame:
    return pd.DataFrame(
        {"dir": 0, "stop_dist": np.nan, "target_dist": np.nan, "score": 0.0, "max_hold": 0},
        index=index,
    )


def trend_signals(f: pd.DataFrame, p: TrendParams = DEFAULT_TREND) -> pd.DataFrame:
    c = f["close"]
    fast, slow, htf = ema(c, p.fast), ema(c, p.slow), ema(c, p.htf)
    atr = f["atr"]
    slope = (slow - slow.shift(p.slope_bars)) / atr
    touched_up = (f["low"] <= fast).rolling(p.pullback_bars, min_periods=1).max() > 0
    touched_dn = (f["high"] >= fast).rolling(p.pullback_bars, min_periods=1).max() > 0
    up = (c > htf) & (fast > slow) & (slope >= p.min_slope_atr) & touched_up & (c > fast)
    dn = (c < htf) & (fast < slow) & (slope <= -p.min_slope_atr) & touched_dn & (c < fast)
    s = _signal_frame(f.index)
    sig = up | dn
    s.loc[up, "dir"] = 1
    s.loc[dn, "dir"] = -1
    s.loc[sig, "stop_dist"] = p.stop_atr * atr[sig]
    s.loc[sig, "target_dist"] = p.target_atr * atr[sig]
    sep = ((fast - slow).abs() / atr).clip(upper=2.0) / 2.0
    s.loc[sig, "score"] = (0.55 + 0.25 * sep + 0.2 * (slope.abs() / 3).clip(upper=1.0))[sig]
    s.loc[sig, "max_hold"] = p.max_hold
    return s


def momentum_signals(f: pd.DataFrame, p: MomentumParams = DEFAULT_MOMENTUM) -> pd.DataFrame:
    c = f["close"]
    htf = ema(c, p.htf)
    hi = f["high"].rolling(p.channel, min_periods=p.channel).max().shift(1)
    lo = f["low"].rolling(p.channel, min_periods=p.channel).min().shift(1)
    expanding = f["atr"] > p.atr_expansion * f["atr_sma"]
    r = f["rsi"]
    up = (c > hi) & (c > htf) & expanding & (r >= p.rsi_min) & (r <= p.rsi_max)
    dn = (c < lo) & (c < htf) & expanding & (r <= 100 - p.rsi_min) & (r >= 100 - p.rsi_max)
    s = _signal_frame(f.index)
    sig = up | dn
    s.loc[up, "dir"] = 1
    s.loc[dn, "dir"] = -1
    s.loc[sig, "stop_dist"] = p.stop_atr * f["atr"][sig]
    s.loc[sig, "target_dist"] = p.target_atr * f["atr"][sig]
    strength = ((r - 50).abs() / 30).clip(upper=1.0)
    s.loc[sig, "score"] = (0.55 + 0.25 * strength + 0.2 * (f["atr"] / f["atr_sma"] - 1).clip(0, 1))[
        sig
    ]
    s.loc[sig, "max_hold"] = p.max_hold
    return s


def mean_reversion_signals(
    f: pd.DataFrame, p: MeanReversionParams = DEFAULT_MEAN_REVERSION
) -> pd.DataFrame:
    c = f["close"]
    bb = bollinger(c, 20, p.band_k, 0)
    lower, upper, mid = bb["bb_lower"], bb["bb_upper"], bb["bb_middle"]
    ranging = f["er"] <= p.max_er
    r = f["rsi"]
    # confirmed rejection: previous close outside the band, this close back inside
    up = ranging & (c.shift(1) < lower.shift(1)) & (c > lower) & (r.shift(1) <= p.rsi_extreme)
    dn = ranging & (c.shift(1) > upper.shift(1)) & (c < upper) & (r.shift(1) >= 100 - p.rsi_extreme)
    atr = f["atr"]
    swing_lo = f["low"].rolling(3, min_periods=1).min()
    swing_hi = f["high"].rolling(3, min_periods=1).max()
    stop_up = (c - swing_lo) + p.stop_buffer_atr * atr
    stop_dn = (swing_hi - c) + p.stop_buffer_atr * atr
    tgt = (mid - c).abs()
    s = _signal_frame(f.index)
    long_ok = up & (tgt >= p.min_rr * stop_up)
    short_ok = dn & (tgt >= p.min_rr * stop_dn)
    s.loc[long_ok, "dir"] = 1
    s.loc[short_ok, "dir"] = -1
    s.loc[long_ok, "stop_dist"] = stop_up[long_ok]
    s.loc[short_ok, "stop_dist"] = stop_dn[short_ok]
    sig = long_ok | short_ok
    s.loc[sig, "target_dist"] = tgt[sig]
    depth = ((50 - r.shift(1)).abs() / 50).clip(upper=1.0)
    s.loc[sig, "score"] = (0.55 + 0.45 * depth)[sig]
    s.loc[sig, "max_hold"] = p.max_hold
    return s


def regime_frame(f: pd.DataFrame, p: RegimeParams = DEFAULT_REGIME) -> pd.DataFrame:
    """Regime label and veto for every bar."""
    er, pct = f["er"], f["atr_pct"]
    spread_ratio = f["spread"] / f["atr"]
    volatile = pct >= p.atr_pct_high
    quiet = pct <= p.atr_pct_low
    wide = spread_ratio > p.max_spread_atr
    regime = np.where(er >= p.er_trend, "trending", np.where(er <= p.er_range, "ranging", "mixed"))
    regime = np.where(volatile, "volatile", np.where(quiet, "quiet", regime))
    veto = volatile | quiet | wide.fillna(False) | ~f["tradable"]
    return pd.DataFrame({"regime": regime, "veto": veto.astype(bool)}, index=f.index)
