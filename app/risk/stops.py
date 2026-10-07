"""Long stop-loss levels and their CLOSE-based evaluation on daily bars.

Daily OHLC does not say in which order the high, the low and the close happened, so this module
never infers intraday sequencing:
- a stop is TRIGGERED only when the session CLOSE is at or below the level; the exit is decided
  at that close and executed at the next session open (Phase 8 convention), so gap risk beyond
  the level is borne, never assumed away;
- a LOW at or below the level with a close above it is reported as an `intraday_touch`
  (diagnostic only, never an exit), because without intraday data we cannot know whether a
  stop order would have filled, at which price, or before/after the high;
- intraday stop orders (fills at the stop price during the session) are unsupported.
"""

import math

from app.risk.config import StopSpec


def stop_level(spec: StopSpec, *, entry_price: float, atr_at_decision: float) -> float:
    """Level set once, at entry. ATR is the value known at the entry decision (close before
    the entry fill). NaN when no stop applies or the ATR is undefined (reported, not guessed)."""
    if spec.method == "none":
        return math.nan
    if spec.method == "fixed_pct":
        return entry_price * (1 - spec.pct)
    if spec.method == "atr":
        if not (math.isfinite(atr_at_decision) and atr_at_decision > 0):
            return math.nan
        return entry_price - spec.atr_multiple * atr_at_decision
    raise ValueError(f"unknown stop method {spec.method!r}")


def evaluate_stop(level: float, *, low: float, close: float) -> tuple[bool, bool]:
    """(triggered_at_close, intraday_touch_only)."""
    if not math.isfinite(level):
        return False, False
    triggered = close <= level
    return triggered, (not triggered) and low <= level
