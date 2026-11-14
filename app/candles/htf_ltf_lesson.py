"""Aula 02 vocabulary and causal research observations. Never an order signal.

Only closed bars available at as_of are inspected. Numeric pivot/reference choices
are local research conventions; the slides do not fully specify an algorithm.
"""

from typing import Any

import pandas as pd

VERSION = "aula02-htf-ltf/1.0.0"
SOURCE_SHA256 = "4e53c8c39e0944751180398927e13ea31e4dcd2f1eb7143fc3a960971804fa1c"
LESSONS = {
    "multi_timeframe": {
        "pages": [9, 22, 30, 34],
        "rule": "D1 contexto; H4 direção/liquidez; H1 refinamento; M15/M5/M1 confirmação.",
    },
    "structure": {
        "pages": [10, 11, 12, 13],
        "rule": (
            "Distinguir rompimento de continuação e mudança de estrutura; "
            "confirmar pivôs antes de usar."
        ),
    },
    "ote": {
        "pages": [14],
        "rule": (
            "Retração da expansão: 0.62 a 0.79, nível ilustrado 0.705; "
            "não constitui entrada isolada."
        ),
    },
    "asian_range": {
        "pages": [16, 17, 35],
        "rule": "Range Sydney, sweep em Tóquio, MSS, retração OTE na região adequada.",
    },
    "london_killzone": {
        "pages": [18, 19, 35],
        "rule": "Range asiático, sweep em Londres, MSS, retração OTE.",
    },
    "ny_killzone": {
        "pages": [20, 21, 35],
        "rule": "Liquidez prévia/Londres, sweep NY, MSS, OTE; horários divergem entre slides.",
    },
    "crt": {
        "pages": [24, 25, 26, 27, 28],
        "rule": (
            "Vela de referência: máxima, mínima e 50%; sweep, retorno, MSS "
            "e objetivo na extremidade oposta, com fluxo HTF."
        ),
    },
    "liquidity": {
        "pages": [29, 30, 31, 32, 33, 34],
        "rule": (
            "BSL/SSL, liquidez interna/externa, extremos fortes/fracos e zonas "
            "de interesse são contexto, não ordens observadas."
        ),
    },
    "confluence": {
        "pages": [7, 27, 34],
        "rule": "OB, FVG, SMT e PO3 são citados, mas faltam critérios numéricos completos.",
    },
}
GAPS = [
    "Seleção da vela CRT, fronteiras H4/D1 e fuso do gráfico não especificados.",
    "Pivôs, MSS/BOS/CHoCH, força do deslocamento e validade do sweep sem tolerâncias numéricas.",
    "NY: p20 indica início 10h30; p35 Open 11h30-12h30 (Brasil); DST sem regra.",
    "Stop, expiração, custos, filtros macro e resolução intrabar não completamente definidos.",
    "SMT/PO3/OB/FVG citados não equivalem a modelos completos demonstrados.",
]


def curriculum(role: str) -> dict[str, Any]:
    """Machine-readable role briefing; learning means explicit knowledge, not fitted weights."""
    focus = {
        "reversao": ["structure", "crt", "liquidity", "ote"],
        "continuacao": ["multi_timeframe", "crt", "structure", "ote"],
        "impulso": ["asian_range", "london_killzone", "ny_killzone", "structure"],
        "analista": list(LESSONS),
    }
    if role not in focus:
        raise ValueError("unknown specialist role")
    return {
        "version": VERSION,
        "role": role,
        "source_sha256": SOURCE_SHA256,
        "lessons": {key: LESSONS[key] for key in focus[role]},
        "gaps": GAPS,
        "execution_enabled": False,
        "statistically_approved": False,
        "training_weights_changed": False,
    }


def observe(
    bars: pd.DataFrame, *, as_of: pd.Timestamp, minutes: int = 5, pivot_width: int = 2
) -> dict[str, Any]:
    """Causal single-bar facts, with no strategy approval or order-sized output.

    Convention v1: UTC H4, immediately preceding complete H4 reference; symmetric
    pivots confirmed only after pivot_width right bars. Sweep must close back
    inside. MSS is recorded separately and is not asserted as a completed setup.
    """
    if minutes != 5 or pivot_width < 1:
        raise ValueError("research profile requires M5 and positive pivot width")
    if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
        raise ValueError("timezone-aware DatetimeIndex required")
    now = pd.Timestamp(as_of)
    if now.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    if not bars.index.is_unique or not bars.index.is_monotonic_increasing:
        raise ValueError("ordered unique bars required")
    frame = (
        bars.loc[
            bars.index + pd.Timedelta(minutes=minutes) <= now, ["open", "high", "low", "close"]
        ]
        .astype(float)
        .copy()
    )
    frame.index = pd.DatetimeIndex(frame.index).tz_convert("UTC")
    result: dict[str, Any] = {
        "version": VERSION,
        "as_of": now.isoformat(),
        "execution_enabled": False,
        "statistically_approved": False,
        "profile": f"research-convention-utc-h4-pivot{pivot_width}",
        "facts": [],
        "source_pages": [9, 14, 24, 25, 26, 28],
        "missing": [],
    }
    if frame.empty:
        result["missing"] = ["closed_m5"]
        return result
    import numpy as np

    if not np.isfinite(frame.to_numpy()).all() or (frame.low <= 0).any():
        raise ValueError("finite positive OHLC required")
    if (
        (frame.high < frame[["open", "close", "low"]].max(axis=1))
        | (frame.low > frame[["open", "close"]].min(axis=1))
    ).any():
        raise ValueError("invalid OHLC geometry")
    index = pd.DatetimeIndex(frame.index)
    if not ((index.minute % 5 == 0) & (index.second == 0)).all():
        raise ValueError("M5 grid required")
    latest = frame.iloc[-1]
    end = frame.index[-1]
    # Never bridge a missing bar for LTF geometry.
    recent = frame.iloc[-max(8, 2 * pivot_width + 3) :]
    contiguous = (recent.index.to_series().diff().dropna() == pd.Timedelta(minutes=5)).all()
    if not contiguous:
        result["missing"].append("contiguous_ltf")
    else:
        if len(frame) >= 3:
            a = frame.iloc[-3]
            if latest.low > a.high:
                result["facts"].append(
                    {
                        "kind": "bullish_fvg_geometry",
                        "low": float(a.high),
                        "high": float(latest.low),
                    }
                )
            if latest.high < a.low:
                result["facts"].append(
                    {
                        "kind": "bearish_fvg_geometry",
                        "low": float(latest.high),
                        "high": float(a.low),
                    }
                )
        pivots: dict[str, float] = {}
        # Restrict to the most recent continuous tail, so missing history is never bridged.
        tail = frame.iloc[-96:]
        gaps = tail.index.to_series().diff() != pd.Timedelta(minutes=5)
        if gaps.iloc[1:].any():
            tail = tail.loc[gaps[gaps].index[-1] :]
        for i in range(pivot_width, len(tail) - pivot_width - 1):
            window = tail.iloc[i - pivot_width : i + pivot_width + 1]
            row = tail.iloc[i]
            if row.high == window.high.max() and (window.high == row.high).sum() == 1:
                pivots["high"] = float(row.high)
            if row.low == window.low.min() and (window.low == row.low).sum() == 1:
                pivots["low"] = float(row.low)
        previous = frame.iloc[-2] if len(frame) >= 2 else latest
        for key, direction in (("high", "bullish"), ("low", "bearish")):
            level = pivots.get(key)
            if level is not None and (
                (previous.close <= level < latest.close)
                if key == "high"
                else (previous.close >= level > latest.close)
            ):
                result["facts"].append(
                    {
                        "kind": direction + "_structure_break",
                        "level": level,
                        "classification": "MSS_vs_BOS_requires_prior_trend",
                    }
                )
    ref_open = end.floor("4h") - pd.Timedelta(hours=4)
    expected = pd.date_range(ref_open, periods=48, freq="5min", tz="UTC")
    ref = frame.reindex(expected)
    if ref.isna().any().any():
        result["missing"].append("complete_previous_h4")
        return result
    high, low = float(ref.high.max()), float(ref.low.min())
    result["reference"] = {
        "open_at": ref_open.isoformat(),
        "high": high,
        "low": low,
        "equilibrium": (high + low) / 2,
        "selection_is_convention": True,
    }
    if high > low:
        result["location"] = (
            "premium"
            if latest.close > (high + low) / 2
            else "discount"
            if latest.close < (high + low) / 2
            else "equilibrium"
        )
    if latest.high > high and low < latest.close < high:
        result["facts"].append(
            {
                "kind": "crt_upper_sweep_return",
                "direction": "bearish",
                "objective": low,
                "mss_confirmed": False,
                "setup_complete": False,
            }
        )
    if latest.low < low and low < latest.close < high:
        result["facts"].append(
            {
                "kind": "crt_lower_sweep_return",
                "direction": "bullish",
                "objective": high,
                "mss_confirmed": False,
                "setup_complete": False,
            }
        )
    result["bar_open"] = end.isoformat()
    return result


def ote_zone(low: float, high: float, direction: str) -> tuple[float, float]:
    """Illustrated retracement geometry only; anchors must have been fixed causally."""
    import math

    if not all(math.isfinite(x) for x in (low, high)) or not 0 < low < high:
        raise ValueError("positive finite expansion range required")
    width = high - low
    if direction == "bullish":
        return high - 0.79 * width, high - 0.62 * width
    if direction == "bearish":
        return low + 0.62 * width, low + 0.79 * width
    raise ValueError("direction must be bullish or bearish")
