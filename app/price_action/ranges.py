"""Consolidation (range) detection and range expansion/contraction. Vectorised, past-only.

Window (W = range_window_bars), at bar t over bars t-W+1 .. t:
    window_high = max(high), window_low = min(low)
    window_width_pct = (window_high - window_low) / ((window_high + window_low) / 2)
    in_consolidation = window_width_pct <= range_max_width_pct
A consolidation EPISODE is a run of consecutive in_consolidation bars. While it lasts:
    range_start = first bar of the window of the episode's first bar
    range_high / range_low = extremes of all bars from range_start to t (union, may exceed
    the window threshold as the episode extends), bar_count = bars from range_start to t.

Expansion / contraction (N = expansion_lookback):
    candle_range_ratio = (high - low)[t] / mean((high - low)[t-N .. t-1])
    window_width_ratio = window_width_pct[t] / window_width_pct[t-W]
    state = expansion if ratio >= expansion_min_ratio, contraction if <= contraction_max_ratio,
    normal otherwise, unknown without history. Magnitude only: direction is reported separately
    (candle direction; sign of close[t] - close[t-W]) and is not a bullish/bearish judgement.
"""

import numpy as np
import pandas as pd

from app.price_action.config import PriceActionConfig


def _state(ratio: pd.Series, cfg: PriceActionConfig) -> pd.Series:
    return pd.Series(
        np.select(
            [ratio >= cfg.expansion_min_ratio, ratio <= cfg.contraction_max_ratio, ratio.notna()],
            ["expansion", "contraction", "normal"],
            "unknown",
        ),
        index=ratio.index,
    )


def compute_ranges(
    bars: pd.DataFrame, candle_range: pd.Series, cfg: PriceActionConfig
) -> pd.DataFrame:
    w = cfg.range_window_bars
    high, low, close = bars["high"], bars["low"], bars["close"]
    win_high = high.rolling(w, min_periods=w).max()
    win_low = low.rolling(w, min_periods=w).min()
    win_width = ((win_high - win_low) / ((win_high + win_low) / 2)).round(10)
    in_cons = (win_width <= cfg.range_max_width_pct).fillna(False).astype(bool)

    episode = (in_cons & ~in_cons.shift(1, fill_value=False)).cumsum().where(in_cons)
    run = in_cons.groupby(episode).cumcount() + 1
    positions = pd.Series(np.arange(len(bars)), index=bars.index)
    start_pos = (positions - (run - 1) - (w - 1)).where(in_cons)
    range_high = win_high.where(in_cons).groupby(episode).cummax()
    range_low = win_low.where(in_cons).groupby(episode).cummin()
    range_start = pd.Series(
        [bars.index[int(p)] if not np.isnan(p) else pd.NaT for p in start_pos],
        index=bars.index,
        dtype="datetime64[ns, UTC]",
    )

    prior_mean = (
        candle_range.shift(1)
        .rolling(cfg.expansion_lookback, min_periods=cfg.expansion_lookback)
        .mean()
    )
    candle_ratio = (candle_range / prior_mean.where(prior_mean > 0)).round(10)
    width_ratio = (win_width / win_width.shift(w).where(win_width.shift(w) > 0)).round(10)
    window_move = close - close.shift(w)

    range_mid = (range_high + range_low) / 2
    return pd.DataFrame(
        {
            "window_high": win_high,
            "window_low": win_low,
            "window_width_pct": win_width,
            "in_consolidation": in_cons,
            "range_start": range_start,
            "range_high": range_high,
            "range_low": range_low,
            "range_width": range_high - range_low,
            "range_width_pct": ((range_high - range_low) / range_mid).round(10),
            "range_bar_count": (run + w - 1).where(in_cons).astype("Int64"),
            "candle_range_ratio": candle_ratio,
            "candle_range_state": _state(candle_ratio, cfg),
            "window_width_ratio": width_ratio,
            "window_width_state": _state(width_ratio, cfg),
            "window_direction": pd.Series(
                np.select(
                    [window_move > 0, window_move < 0, window_move.notna()],
                    ["up", "down", "flat"],
                    "unknown",
                ),
                index=bars.index,
            ),
        },
        index=bars.index,
    )
