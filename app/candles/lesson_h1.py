"""Frozen H1 adaptation of Aula02; an extra veto, never a stand-alone signal.

This is not the illustrated H4/M5 model. UTC H4, strict 3-bar structural close,
8-hour sequence and OTE rejection are explicit local conventions. No session
expansion: the existing H1 calendar, risk and execution still decide eligibility.
"""

from typing import Any

import numpy as np
import pandas as pd

from app.candles.htf_ltf_lesson import SOURCE_SHA256, ote_zone

VERSION = "aula02-h1-veto/1.0.0"


def analyze_h1(bars: pd.DataFrame) -> dict[str, Any]:
    """Input must already be closed H1 bars. Latest bar never defines past anchors."""
    out: dict[str, Any] = {
        "version": VERSION,
        "source_sha256": SOURCE_SHA256,
        "source_pages": [9, 14, 24, 25, 28],
        "adaptation": "H4/H1; not H4/M5",
        "approved": False,
        "weights_changed": False,
        "ready": False,
        "direction": 0,
        "reason": "insufficient_complete_h1",
        "sequence": None,
    }
    if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
        raise ValueError("timezone-aware H1 required")
    if not bars.index.is_monotonic_increasing or not bars.index.is_unique:
        raise ValueError("unique sorted H1 required")
    frame = bars[["open", "high", "low", "close"]].astype(float).copy()
    frame.index = pd.DatetimeIndex(frame.index).tz_convert("UTC")
    if not np.isfinite(frame.to_numpy()).all():
        raise ValueError("finite OHLC required")
    if (frame.low <= 0).any() or (
        (frame.high < frame.max(axis=1)) | (frame.low > frame.min(axis=1))
    ).any():
        raise ValueError("invalid OHLC")
    if len(frame) < 16:
        return out
    index = pd.DatetimeIndex(frame.index)
    if not ((index.minute == 0) & (index.second == 0)).all():
        raise ValueError("H1 grid required")
    end = index[-1]
    # Missing bars invalidate the entire 16-hour context, never interpolate.
    window = frame.reindex(pd.date_range(end - pd.Timedelta(hours=15), end, freq="h"))
    if window.isna().any().any():
        out["reason"] = "missing_h1_context"
        return out
    out["reason"] = "no_ordered_sweep_mss_ote"
    latest = frame.iloc[-1]
    for i in range(len(frame) - 2, max(3, len(frame) - 9) - 1, -1):
        sweep = frame.iloc[i]
        swept_at = frame.index[i]
        reference_open = swept_at.floor("4h") - pd.Timedelta(hours=4)
        reference = frame.reindex(pd.date_range(reference_open, periods=4, freq="h"))
        if reference.isna().any().any():
            continue
        high, low = float(reference.high.max()), float(reference.low.min())
        # Restrict to SELL-compatible bearish H4 flow; bullish never becomes a SELL.
        if not (
            reference.iloc[-1].close < reference.iloc[0].open
            and sweep.high > high
            and low < sweep.close < high
            and sweep.low >= low
        ):
            continue
        structure = float(frame.iloc[i - 3 : i].low.min())
        # MSS convention: close below a pre-existing 3-bar low on a later bar.
        for j in range(i + 1, len(frame) - 1):
            mss = frame.iloc[j]
            if not (mss.close < structure and mss.close < mss.open):
                continue
            origin = float(frame.iloc[i : j + 1].high.max())
            base = float(frame.iloc[i : j + 1].low.min())
            if origin <= base:
                continue
            zone_low, zone_high = ote_zone(base, origin, "bearish")
            # Invalidate on a new expansion low before retest: anchors cannot drift.
            after = frame.iloc[j + 1 :]
            if (after.high >= origin).any() or (frame.iloc[j + 1 : -1].low < base).any():
                break
            if not (
                latest.high >= zone_low
                and latest.high <= zone_high
                and latest.close < zone_low
                and latest.close < latest.open
                and latest.close < high
                and latest.close > low
            ):
                break
            out.update(
                ready=True,
                direction=-1,
                reason="bearish_htf_sweep_mss_ote_rejection",
                sequence={
                    "sweep_at": swept_at.isoformat(),
                    "mss_at": frame.index[j].isoformat(),
                    "reference_open": reference_open.isoformat(),
                    "reference_high": high,
                    "reference_low": low,
                    "structure_low": structure,
                    "expansion_high": origin,
                    "expansion_low": base,
                    "ote_low": zone_low,
                    "ote_high": zone_high,
                    "entry_bar": end.isoformat(),
                },
            )
            return out
    return out


def permits(context: dict[str, Any], direction: int, model_agrees: bool) -> bool:
    """No missing metadata fallback when this explicitly enabled profile is required."""
    lesson = context.get("lesson_h1", {})
    return bool(
        lesson.get("version") == VERSION
        and lesson.get("ready") is True
        and lesson.get("direction") == direction == -1
        and model_agrees
    )
