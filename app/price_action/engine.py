"""PriceActionEngine: closed OHLCV bars -> market structure, zones, events, outcomes.

Describes what price is doing across candles. It makes NO trading decisions.

Point-in-time processing, for each bar t in order:
  1. EVENTS at t (breakout, breakdown, rejection, sweep) are evaluated against zones that were
     available at the close of t-1.
  2. Pending breakouts/breakdowns from earlier bars are updated: RETEST events (observations)
     and OUTCOMES (failed / held), each stamped with the bar at which they became known.
  3. Pivots confirmed at t (pivot at t - swing_right_bars) are added: structure labels, zones.
  4. Touches of zones available before t are recorded.
  5. The STATE row for t (structure, trend, nearest zones, range state) is emitted.
Nothing at step t reads bars after t, so state(t) is identical whether or not later bars exist.
"""

from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.candles.config import CandleConfig
from app.candles.geometry import OHLCV, compute_geometry
from app.price_action.config import ENGINE_VERSION, PriceActionConfig
from app.price_action.ranges import compute_ranges
from app.price_action.swings import detect_swings
from app.price_action.zones import ZoneBook

EVENT_COLUMNS = (
    "event_id",
    "event_type",
    "ts",
    "available_at",
    "direction",
    "zone_id",
    "reference_low",
    "reference_high",
    "reference_level",
    "close",
    "distance_pct",
    "wick_ratio",
    "confirmation_type",
    "related_event_id",
)
OUTCOME_COLUMNS = (
    "event_id",
    "event_type",
    "event_ts",
    "outcome",
    "outcome_at",
    "bars_to_outcome",
    "reference_level",
)

STRUCTURE_TO_TREND = {
    "uptrend": "up",
    "downtrend": "down",
    "range": "sideways",
    "transition": "unclear",
    "insufficient_data": "insufficient_data",
}
TREND_CONSISTENT = {
    "up": {"HH", "HL"},
    "down": {"LH", "LL"},
    "sideways": {"LH", "EH", "HL", "EL"},
}


# Explicit dtypes: the representation of state(T) must not depend on whether later rows exist
# (e.g. an all-None column would otherwise be `object` in a truncated run and float in a full one).
_UTC = "datetime64[ns, UTC]"
STATE_DTYPES = {
    "last_high_label": "string",
    "last_low_label": "string",
    "trend_quality": "float64",
    "last_swing_high": "float64",
    "last_swing_low": "float64",
    "support_zone_id": "Int64",
    "support_low": "float64",
    "support_high": "float64",
    "support_distance_pct": "float64",
    "resistance_zone_id": "Int64",
    "resistance_low": "float64",
    "resistance_high": "float64",
    "resistance_distance_pct": "float64",
    "active_zones": "int64",
}
EVENT_DTYPES = {
    "event_id": "int64",
    "ts": _UTC,
    "available_at": _UTC,
    "zone_id": "Int64",
    "reference_low": "float64",
    "reference_high": "float64",
    "reference_level": "float64",
    "close": "float64",
    "distance_pct": "float64",
    "wick_ratio": "float64",
    "related_event_id": "Int64",
}
OUTCOME_DTYPES = {
    "event_id": "int64",
    "event_ts": _UTC,
    "outcome_at": _UTC,
    "bars_to_outcome": "Int64",
    "reference_level": "float64",
}


def _typed(frame: pd.DataFrame, dtypes: dict[str, str]) -> pd.DataFrame:
    for col, dtype in dtypes.items():
        if dtype == _UTC:
            frame[col] = pd.to_datetime(frame[col], utc=True)
        else:
            cleaned = frame[col].astype(object).where(frame[col].notna(), None)
            frame[col] = pd.Series(cleaned.tolist(), index=frame.index, dtype=dtype)
    return frame


def _dist(price: float, levels: np.ndarray) -> np.ndarray:
    """price / level - 1, rounded to 1e-10 so exact threshold boundaries are stable."""
    return np.round(price / levels - 1, 10)


def _pending(
    event_id: int, event_type: str, direction: str, idx: int, level: float
) -> dict[str, Any]:
    return {"event_id": event_id, "type": event_type, "dir": direction, "idx": idx,
            "level": level, "retest": True}  # fmt: skip


def classify_structure(high_labels: list[str], low_labels: list[str], n: int) -> str:
    """Explicit rules on the last n labels of each kind (see docs/price-action-engine.md)."""
    if len(high_labels) < n or len(low_labels) < n:
        return "insufficient_data"
    highs, lows = set(high_labels[-n:]), set(low_labels[-n:])
    if highs == {"HH"} and lows == {"HL"}:
        return "uptrend"
    if highs == {"LH"} and lows == {"LL"}:
        return "downtrend"
    if highs <= {"LH", "EH"} and lows <= {"HL", "EL"}:
        return "range"
    return "transition"


@dataclass(frozen=True)
class PriceActionAnalysis:
    swings: pd.DataFrame  # confirmed pivots; available_at = confirmed_at
    state: pd.DataFrame  # one row per bar; available at that bar's close
    zones: pd.DataFrame  # active zones as of the LAST bar
    events: pd.DataFrame  # observations, each with available_at
    outcomes: pd.DataFrame  # later outcomes of breakouts/breakdowns, with outcome_at
    engine_version: str
    config_fingerprint: str

    def state_as_of(self, ts: pd.Timestamp) -> pd.Series:
        """State after the last bar whose ts <= `ts` closed (multi-timeframe alignment hook)."""
        eligible = self.state.loc[: pd.Timestamp(ts)]
        if eligible.empty:
            raise LookupError(f"no state at or before {ts}")
        return eligible.iloc[-1]


class PriceActionEngine:
    def __init__(self, config: PriceActionConfig | None = None) -> None:
        self.config = config or PriceActionConfig()

    @staticmethod
    def _check_input(bars: pd.DataFrame) -> None:
        missing = set(OHLCV) - set(bars.columns)
        if missing:
            raise ValueError(f"bars missing columns: {sorted(missing)}")
        idx = bars.index
        if not isinstance(idx, pd.DatetimeIndex) or idx.tz is None or str(idx.tz) != "UTC":
            raise ValueError("bars must be indexed by a UTC DatetimeIndex")
        if not idx.is_monotonic_increasing or not idx.is_unique:
            raise ValueError("bars index must be strictly increasing (sorted, no duplicates)")
        if "is_closed" in bars.columns and not bars["is_closed"].astype(bool).all():
            raise ValueError("engine accepts only closed bars; filter is_closed first")
        if bars[list(OHLCV)].isna().any().any():
            raise ValueError("bars contain missing OHLCV values; validate data first")

    def analyze(
        self, bars: pd.DataFrame, *, instrument_id: int | None = None, timeframe: str = "1d"
    ) -> PriceActionAnalysis:
        self._check_input(bars)
        cfg = self.config
        index = pd.DatetimeIndex(bars.index)
        n = len(bars)
        h, lo, c = (bars[col].to_numpy(dtype=np.float64) for col in ("high", "low", "close"))

        # Candle geometry is reused from the Candlestick Engine (no duplicate formulas).
        geometry = compute_geometry(bars, CandleConfig())
        upper_wick = geometry["upper_wick_ratio"].to_numpy(dtype=np.float64)
        lower_wick = geometry["lower_wick_ratio"].to_numpy(dtype=np.float64)

        swings = detect_swings(bars, cfg)
        confirmed: dict[int, list[dict[str, Any]]] = {}
        swing_records: list[dict[str, Any]] = swings.to_dict("records")  # type: ignore[assignment]
        for swing in swing_records:
            confirmed.setdefault(int(swing["confirmed_idx"]), []).append(swing)

        book = ZoneBook(cfg.zone_tolerance_pct, cfg.zone_max_width_pct)
        events: list[dict[str, Any]] = []
        outcomes: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        high_labels: list[str] = []
        low_labels: list[str] = []
        recent: deque[str] = deque(maxlen=cfg.trend_quality_labels)
        last_high: float | None = None
        last_low: float | None = None
        state_rows: list[dict[str, Any]] = []

        def emit(event_type: str, t: int, direction: str, zone_id: int | None, ref_low: float,
                 ref_high: float, level: float, distance: float, wick: float | None,
                 related: int | None = None) -> int:  # fmt: skip
            event_id = len(events)
            events.append(
                {
                    "event_id": event_id,
                    "event_type": event_type,
                    "ts": index[t],
                    "available_at": index[t],
                    "direction": direction,
                    "zone_id": zone_id,
                    "reference_low": ref_low,
                    "reference_high": ref_high,
                    "reference_level": level,
                    "close": c[t],
                    "distance_pct": round(distance, 10),
                    "wick_ratio": wick,
                    "confirmation_type": "close",
                    "related_event_id": related,
                }
            )
            return event_id

        for t in range(n):
            # 1. events vs zones available at the close of t-1
            if t > 0:
                ids, zl, zh, first = book.arrays()
                avail = first <= t - 1
                pc = c[t - 1]
                br_min, sw_min = cfg.breakout_min_distance_pct, cfg.sweep_min_distance_pct
                with np.errstate(divide="ignore", invalid="ignore"):
                    close_vs_high = _dist(c[t], zh)
                    close_vs_low = _dist(c[t], zl)
                    high_vs_high = _dist(h[t], zh)
                    low_vs_low = _dist(lo[t], zl)
                for k in np.flatnonzero(avail & (pc <= zh) & (close_vs_high >= br_min)):
                    eid = emit(
                        "breakout",
                        t,
                        "up",
                        int(ids[k]),
                        zl[k],
                        zh[k],
                        zh[k],
                        close_vs_high[k],
                        None,
                    )
                    pending.append(_pending(eid, "breakout", "up", t, zh[k]))
                for k in np.flatnonzero(avail & (pc >= zl) & (close_vs_low <= -br_min)):
                    eid = emit(
                        "breakdown",
                        t,
                        "down",
                        int(ids[k]),
                        zl[k],
                        zh[k],
                        zl[k],
                        close_vs_low[k],
                        None,
                    )
                    pending.append(_pending(eid, "breakdown", "down", t, zl[k]))
                for k in np.flatnonzero(
                    avail & (pc <= zh) & (high_vs_high >= sw_min) & (c[t] <= zh)
                ):
                    emit(
                        "sweep",
                        t,
                        "up",
                        int(ids[k]),
                        zl[k],
                        zh[k],
                        zh[k],
                        high_vs_high[k],
                        upper_wick[t],
                    )
                for k in np.flatnonzero(
                    avail & (pc >= zl) & (low_vs_low <= -sw_min) & (c[t] >= zl)
                ):
                    emit(
                        "sweep",
                        t,
                        "down",
                        int(ids[k]),
                        zl[k],
                        zh[k],
                        zl[k],
                        low_vs_low[k],
                        lower_wick[t],
                    )
                rej_up = round((h[t] - c[t]) / c[t], 10)
                rej_dn = round((c[t] - lo[t]) / c[t], 10)
                if (
                    upper_wick[t] >= cfg.rejection_min_wick_ratio
                    and rej_up >= cfg.rejection_min_distance_pct
                ):
                    for k in np.flatnonzero(avail & (zl > pc) & (h[t] >= zl) & (c[t] < zl)):
                        emit(
                            "rejection",
                            t,
                            "down",
                            int(ids[k]),
                            zl[k],
                            zh[k],
                            zl[k],
                            rej_up,
                            upper_wick[t],
                        )
                if (
                    lower_wick[t] >= cfg.rejection_min_wick_ratio
                    and rej_dn >= cfg.rejection_min_distance_pct
                ):
                    for k in np.flatnonzero(avail & (zh < pc) & (lo[t] <= zh) & (c[t] > zh)):
                        emit(
                            "rejection",
                            t,
                            "up",
                            int(ids[k]),
                            zl[k],
                            zh[k],
                            zh[k],
                            rej_dn,
                            lower_wick[t],
                        )

            # 2. retests and outcomes of earlier breaks (never the bar of the break itself)
            still: list[dict[str, Any]] = []
            for p in pending:
                if p["idx"] >= t:
                    still.append(p)
                    continue
                age, level, up = t - p["idx"], p["level"], p["dir"] == "up"
                failed = c[t] < level if up else c[t] > level
                if failed or age >= cfg.outcome_window_bars:
                    outcomes.append(
                        {
                            "event_id": p["event_id"],
                            "event_type": p["type"],
                            "event_ts": index[p["idx"]],
                            "outcome": "failed" if failed else "held",
                            "outcome_at": index[t],
                            "bars_to_outcome": age,
                            "reference_level": level,
                        }
                    )
                    continue
                if p["retest"] and age <= cfg.retest_window_bars:
                    dist = round((lo[t] if up else h[t]) / level - 1, 10)
                    tol = cfg.retest_tolerance_pct
                    if (dist <= tol) if up else (dist >= -tol):
                        emit(
                            "retest",
                            t,
                            p["dir"],
                            None,
                            level,
                            level,
                            level,
                            dist,
                            None,
                            p["event_id"],
                        )
                        p["retest"] = False
                still.append(p)
            pending = still

            # 3. pivots confirmed at t
            for rec in confirmed.get(t, []):
                label = rec["label"]
                if isinstance(label, str):  # the first pivot of each kind has no label
                    (high_labels if rec["kind"] == "high" else low_labels).append(label)
                    recent.append(label)
                if rec["kind"] == "high":
                    last_high = rec["price"]
                else:
                    last_low = rec["price"]
                book.add_pivot(rec["price"], rec["kind"], rec["pivot_idx"], rec["swing_id"], t)

            # 4. touches of zones available before t
            book.record_touches(t, lo[t], h[t])

            # 5. state after bar t
            structure = classify_structure(high_labels, low_labels, cfg.structure_min_labels)
            trend = STRUCTURE_TO_TREND[structure]
            consistent = TREND_CONSISTENT.get(trend)
            quality = (
                round(sum(lbl in consistent for lbl in recent) / len(recent), 6)
                if consistent and recent
                else np.nan
            )
            ids, zl, zh, first = book.arrays()
            avail_now = first <= t
            below = avail_now & (zh < c[t])
            above = avail_now & (zl > c[t])
            inside = avail_now & (zl <= c[t]) & (zh >= c[t])
            sup = int(np.flatnonzero(below)[np.argmax(zh[below])]) if below.any() else None
            res = int(np.flatnonzero(above)[np.argmin(zl[above])]) if above.any() else None
            state_rows.append(
                {
                    "structure": structure,
                    "trend": trend,
                    "trend_quality": quality,
                    "trend_evidence": ",".join(recent),
                    "last_high_label": high_labels[-1] if high_labels else None,
                    "last_low_label": low_labels[-1] if low_labels else None,
                    "last_swing_high": last_high,
                    "last_swing_low": last_low,
                    "support_zone_id": None if sup is None else int(ids[sup]),
                    "support_low": np.nan if sup is None else zl[sup],
                    "support_high": np.nan if sup is None else zh[sup],
                    "support_distance_pct": np.nan
                    if sup is None
                    else round(c[t] / zh[sup] - 1, 10),
                    "resistance_zone_id": None if res is None else int(ids[res]),
                    "resistance_low": np.nan if res is None else zl[res],
                    "resistance_high": np.nan if res is None else zh[res],
                    "resistance_distance_pct": np.nan
                    if res is None
                    else round(zl[res] / c[t] - 1, 10),
                    "inside_zone_ids": ",".join(str(int(z)) for z in ids[inside]),
                    "active_zones": int(avail_now.sum()),
                }
            )

        for p in pending:
            outcomes.append(
                {
                    "event_id": p["event_id"],
                    "event_type": p["type"],
                    "event_ts": index[p["idx"]],
                    "outcome": "pending",
                    "outcome_at": pd.NaT,
                    "bars_to_outcome": None,
                    "reference_level": p["level"],
                }
            )

        state = _typed(pd.DataFrame(state_rows, index=index), STATE_DTYPES)
        ranges = compute_ranges(bars, geometry["range"], cfg)
        state = pd.concat([state, ranges], axis=1)
        state.insert(0, "timeframe", timeframe)
        state.insert(0, "instrument_id", instrument_id)
        state["candle_direction"] = geometry["direction"]

        swings_out = swings.assign(available_at=swings["confirmed_at"])
        events_df = _typed(pd.DataFrame(events, columns=list(EVENT_COLUMNS)), EVENT_DTYPES)
        outcomes_df = _typed(pd.DataFrame(outcomes, columns=list(OUTCOME_COLUMNS)), OUTCOME_DTYPES)
        outcomes_df = outcomes_df.sort_values("event_id", kind="stable").reset_index(drop=True)
        return PriceActionAnalysis(
            swings=swings_out,
            state=state,
            zones=book.to_frame(index),
            events=events_df,
            outcomes=outcomes_df,
            engine_version=ENGINE_VERSION,
            config_fingerprint=cfg.fingerprint(),
        )
