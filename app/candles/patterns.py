"""Deterministic candlestick pattern detectors.

A detector for a k-bar pattern evaluated at row t looks only at rows t-k+1 .. t (plus the
past-only context columns of `geometry`). It never looks at rows after t.

`strength` = geometric quality in [0, 1]: the mean, over the pattern's key criteria, of how far
each value goes beyond its threshold towards an ideal extreme (0 = exactly at the threshold,
1 = ideal). It describes SHAPE only and says nothing about future returns.

`orientation` is the pattern's conventional textbook orientation. It is a label, NOT a
prediction; whether it relates to future returns is for later statistical validation.

Exact definitions: docs/candlestick-engine.md.
"""

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.candles.config import CandleConfig
from app.candles.geometry import ratio

Detector = Callable[[pd.DataFrame, CandleConfig], tuple[pd.Series, pd.Series]]


@dataclass(frozen=True)
class PatternSpec:
    name: str
    family: str  # reversal | continuation | structure | indecision | momentum
    bars: int
    orientation: str | None  # bullish | bearish | neutral; None = taken from candle direction
    context_trend: str | None  # conventional prior trend ("up"/"down"); None = no requirement
    trend_defines_identity: bool  # True: emitted only when the context is met (same shape,
    # different name, e.g. hammer vs hanging man)


# ── helpers ────────────────────────────────────────────────────────────────


def _ge(x: pd.Series, threshold: float, ideal: float) -> pd.Series:
    return ((x - threshold) / (ideal - threshold)).clip(0.0, 1.0)


def _le(x: pd.Series, threshold: float, ideal: float) -> pd.Series:
    return ((threshold - x) / (threshold - ideal)).clip(0.0, 1.0)


def _ge_log(x: pd.Series, ideal: float) -> pd.Series:
    """log(x) / log(ideal), clipped to [0, 1]: 1x -> 0, ideal -> 1 (for heavy-tailed multiples)."""
    logged = pd.Series(np.log(x.where(x > 0)), index=x.index)
    return (logged / float(np.log(ideal))).clip(0.0, 1.0)


def _mean(*parts: pd.Series) -> pd.Series:
    return pd.concat(parts, axis=1).mean(axis=1)


def _sh(g: pd.DataFrame, col: str, k: int) -> pd.Series:
    """Value of `col` k bars BEFORE each row (k >= 0)."""
    return g[col] if k == 0 else g[col].shift(k)


def _usable(g: pd.DataFrame, bars: int) -> pd.Series:
    ok = g["usable"].astype(bool)
    for k in range(1, bars):
        ok = ok & g["usable"].shift(k, fill_value=False).astype(bool)
    return ok


def _is(g: pd.DataFrame, direction: str, k: int = 0) -> pd.Series:
    return _sh(g, "direction", k) == direction


# ── single candle ─────────────────────────────────────────────────────────


def doji(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    br = g["body_ratio"]
    mask = _usable(g, 1) & (br <= cfg.doji_max_body_ratio)
    return mask, _le(br, cfg.doji_max_body_ratio, 0.0)


def spinning_top(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    br, uw, lw = g["body_ratio"], g["upper_wick_ratio"], g["lower_wick_ratio"]
    w = cfg.spinning_top_min_wick_ratio
    mask = (
        _usable(g, 1)
        & (br > cfg.doji_max_body_ratio)
        & (br <= cfg.spinning_top_max_body_ratio)
        & (uw >= w)
        & (lw >= w)
    )
    return mask, _mean(_ge(uw, w, 0.5), _ge(lw, w, 0.5))


def marubozu(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    br = g["body_ratio"]
    mw = cfg.marubozu_max_wick_ratio
    mask = (
        _usable(g, 1)
        & (br >= cfg.marubozu_min_body_ratio)
        & (g["upper_wick_ratio"] <= mw)
        & (g["lower_wick_ratio"] <= mw)
    )
    return mask, _ge(br, cfg.marubozu_min_body_ratio, 1.0)


def long_body(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    br = g["body_ratio"]
    multiple = ratio(g["abs_body"], g["avg_body_prior"])
    mask = (
        _usable(g, 1)
        & (br >= cfg.long_body_min_body_ratio)
        & (multiple >= cfg.long_body_min_multiple)
    )
    m = cfg.long_body_min_multiple
    return mask, _mean(_ge(multiple, m, 2 * m), _ge(br, cfg.long_body_min_body_ratio, 1.0))


def _long_lower_wick_shape(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    br, uw, lw = g["body_ratio"], g["upper_wick_ratio"], g["lower_wick_ratio"]
    mask = (
        _usable(g, 1)
        & (br <= cfg.hammer_max_body_ratio)
        & (lw >= cfg.hammer_min_long_wick_ratio)
        & (uw <= cfg.hammer_max_short_wick_ratio)
        & (g["lower_wick"] >= cfg.hammer_min_wick_body_multiple * g["abs_body"])
    )
    strength = _mean(
        _ge(lw, cfg.hammer_min_long_wick_ratio, 1.0),
        _le(uw, cfg.hammer_max_short_wick_ratio, 0.0),
        _le(br, cfg.hammer_max_body_ratio, 0.0),
    )
    return mask, strength


def _long_upper_wick_shape(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    mirrored = g.rename(
        columns={
            "upper_wick_ratio": "lower_wick_ratio",
            "lower_wick_ratio": "upper_wick_ratio",
            "upper_wick": "lower_wick",
            "lower_wick": "upper_wick",
        }
    )
    return _long_lower_wick_shape(mirrored, cfg)


# ── two candles ───────────────────────────────────────────────────────────


def _engulfing(g: pd.DataFrame, cfg: CandleConfig, bullish: bool) -> tuple[pd.Series, pd.Series]:
    o, c, ab = g["open"], g["close"], g["abs_body"]
    po, pc, pab = _sh(g, "open", 1), _sh(g, "close", 1), _sh(g, "abs_body", 1)
    prev_dir, cur_dir = ("bearish", "bullish") if bullish else ("bullish", "bearish")
    covers = (o <= pc) & (c >= po) if bullish else (o >= pc) & (c <= po)
    mask = (
        _usable(g, 2)
        & _is(g, prev_dir, 1)
        & (_sh(g, "body_ratio", 1) > cfg.doji_max_body_ratio)
        & _is(g, cur_dir)
        & covers
        & (ab > pab)
    )
    return mask, _ge_log(ratio(ab, pab), cfg.engulfing_ideal_body_multiple)


def bullish_engulfing(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    return _engulfing(g, cfg, bullish=True)


def bearish_engulfing(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    return _engulfing(g, cfg, bullish=False)


def _tweezer(g: pd.DataFrame, cfg: CandleConfig, bottom: bool) -> tuple[pd.Series, pd.Series]:
    col = "low" if bottom else "high"
    prev_dir, cur_dir = ("bearish", "bullish") if bottom else ("bullish", "bearish")
    mean_range = (g["range"] + _sh(g, "range", 1)) / 2
    diff = ratio((g[col] - _sh(g, col, 1)).abs(), mean_range)
    tol = cfg.tweezer_max_diff_range_frac
    mask = _usable(g, 2) & _is(g, prev_dir, 1) & _is(g, cur_dir) & (diff <= tol)
    return mask, _le(diff, tol, 0.0)


def tweezer_bottom(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    return _tweezer(g, cfg, bottom=True)


def tweezer_top(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    return _tweezer(g, cfg, bottom=False)


def inside_bar(g: pd.DataFrame, _cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    mask = _usable(g, 2) & (g["high"] < _sh(g, "high", 1)) & (g["low"] > _sh(g, "low", 1))
    return mask, _le(ratio(g["range"], _sh(g, "range", 1)), 1.0, 0.0)


def outside_bar(g: pd.DataFrame, _cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    mask = _usable(g, 2) & (g["high"] > _sh(g, "high", 1)) & (g["low"] < _sh(g, "low", 1))
    return mask, _ge(ratio(g["range"], _sh(g, "range", 1)), 1.0, 3.0)


# ── three candles ─────────────────────────────────────────────────────────


def _star(g: pd.DataFrame, cfg: CandleConfig, morning: bool) -> tuple[pd.Series, pd.Series]:
    o1, c1 = _sh(g, "open", 2), _sh(g, "close", 2)
    o2, c2 = _sh(g, "open", 1), _sh(g, "close", 1)
    c3 = g["close"]
    br1, br2, br3 = _sh(g, "body_ratio", 2), _sh(g, "body_ratio", 1), g["body_ratio"]
    large, small, pen = (
        cfg.large_body_min_ratio,
        cfg.small_body_max_ratio,
        cfg.star_min_penetration,
    )
    if morning:
        outer1, outer3 = "bearish", "bullish"
        star_beyond = np.maximum(o2, c2) <= c1  # star body at/below first close
        penetration = ratio(c3 - c1, o1 - c1)
    else:
        outer1, outer3 = "bullish", "bearish"
        star_beyond = np.minimum(o2, c2) >= c1  # star body at/above first close
        penetration = ratio(c1 - c3, c1 - o1)
    mask = (
        _usable(g, 3)
        & _is(g, outer1, 2)
        & (br1 >= large)
        & (br2 <= small)
        & star_beyond
        & _is(g, outer3)
        & (br3 >= large)
        & (penetration >= pen)
    )
    strength = _mean(
        _ge(br1, large, 1.0), _le(br2, small, 0.0), _ge(br3, large, 1.0), _ge(penetration, pen, 1.0)
    )
    return mask, strength


def morning_star(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    return _star(g, cfg, morning=True)


def evening_star(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    return _star(g, cfg, morning=False)


def _three(g: pd.DataFrame, cfg: CandleConfig, white: bool) -> tuple[pd.Series, pd.Series]:
    direction = "bullish" if white else "bearish"
    wick = "upper_wick_ratio" if white else "lower_wick_ratio"
    large, max_wick = cfg.large_body_min_ratio, cfg.soldiers_max_closing_wick_ratio
    mask = _usable(g, 3)
    for k in (2, 1, 0):
        mask = (
            mask
            & _is(g, direction, k)
            & (_sh(g, "body_ratio", k) >= large)
            & (_sh(g, wick, k) <= max_wick)
        )
    for k in (1, 0):  # bars t-1 and t relative to their predecessor
        o, c = _sh(g, "open", k), _sh(g, "close", k)
        po, pc = _sh(g, "open", k + 1), _sh(g, "close", k + 1)
        progresses = c > pc if white else c < pc
        opens_in_body = (o >= np.minimum(po, pc)) & (o <= np.maximum(po, pc))
        mask = mask & progresses & opens_in_body
    strength = _mean(*(_ge(_sh(g, "body_ratio", k), large, 1.0) for k in (2, 1, 0)))
    return mask, strength


def three_white_soldiers(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    return _three(g, cfg, white=True)


def three_black_crows(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    return _three(g, cfg, white=False)


# ── five candles ──────────────────────────────────────────────────────────


def _three_methods(g: pd.DataFrame, cfg: CandleConfig, rising: bool) -> tuple[pd.Series, pd.Series]:
    direction = "bullish" if rising else "bearish"
    large = cfg.large_body_min_ratio
    h1, l1, c1, ab1 = (_sh(g, col, 4) for col in ("high", "low", "close", "abs_body"))
    br1, br5 = _sh(g, "body_ratio", 4), g["body_ratio"]
    mask = _usable(g, 5) & _is(g, direction, 4) & (br1 >= large)
    for k in (3, 2, 1):  # inner candles: contained in candle 1's range, smaller bodies
        mask = (
            mask
            & (_sh(g, "high", k) <= h1)
            & (_sh(g, "low", k) >= l1)
            & (_sh(g, "abs_body", k) < ab1)
        )
    breaks = g["close"] > c1 if rising else g["close"] < c1
    mask = mask & _is(g, direction) & (br5 >= large) & breaks
    return mask, _mean(_ge(br1, large, 1.0), _ge(br5, large, 1.0))


def rising_three_methods(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    return _three_methods(g, cfg, rising=True)


def falling_three_methods(g: pd.DataFrame, cfg: CandleConfig) -> tuple[pd.Series, pd.Series]:
    return _three_methods(g, cfg, rising=False)


# ── registry ──────────────────────────────────────────────────────────────

S = PatternSpec
REGISTRY: tuple[tuple[PatternSpec, Detector], ...] = (
    # reversal
    (S("hammer", "reversal", 1, "bullish", "down", True), _long_lower_wick_shape),
    (S("hanging_man", "reversal", 1, "bearish", "up", True), _long_lower_wick_shape),
    (S("inverted_hammer", "reversal", 1, "bullish", "down", True), _long_upper_wick_shape),
    (S("shooting_star", "reversal", 1, "bearish", "up", True), _long_upper_wick_shape),
    (S("bullish_engulfing", "reversal", 2, "bullish", "down", False), bullish_engulfing),
    (S("bearish_engulfing", "reversal", 2, "bearish", "up", False), bearish_engulfing),
    (S("morning_star", "reversal", 3, "bullish", "down", False), morning_star),
    (S("evening_star", "reversal", 3, "bearish", "up", False), evening_star),
    (S("tweezer_bottom", "reversal", 2, "bullish", "down", False), tweezer_bottom),
    (S("tweezer_top", "reversal", 2, "bearish", "up", False), tweezer_top),
    # continuation
    (S("three_white_soldiers", "continuation", 3, "bullish", None, False), three_white_soldiers),
    (S("three_black_crows", "continuation", 3, "bearish", None, False), three_black_crows),
    (S("rising_three_methods", "continuation", 5, "bullish", None, False), rising_three_methods),
    (S("falling_three_methods", "continuation", 5, "bearish", None, False), falling_three_methods),
    # structure
    (S("inside_bar", "structure", 2, "neutral", None, False), inside_bar),
    (S("outside_bar", "structure", 2, "neutral", None, False), outside_bar),
    # indecision
    (S("doji", "indecision", 1, "neutral", None, False), doji),
    (S("spinning_top", "indecision", 1, "neutral", None, False), spinning_top),
    # momentum
    (S("marubozu", "momentum", 1, None, None, False), marubozu),
    (S("long_body", "momentum", 1, None, None, False), long_body),
)
