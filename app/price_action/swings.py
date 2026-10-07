"""Pivot (swing) detection with explicit confirmation latency.

Swing high at bar t (k_l = swing_left_bars, k_r = swing_right_bars):
    high[t] >  max(high[t-k_l .. t-1])     (strict: the FIRST of equal highs wins)
    high[t] >= max(high[t+1 .. t+k_r])
Swing low: mirror with lows (low[t] < min(left), low[t] <= min(right)).

The right-hand condition needs k_r FUTURE bars, so a pivot at t is only KNOWN once bar t+k_r
has closed: `confirmed_at = ts[t + k_r]`. Anything point-in-time must use `confirmed_at`,
never `pivot_ts`. A candidate without k_r later bars is not reported at all.

Labels compare a pivot with the previous pivot of the same kind:
    HH / LH / EH (higher / lower / equal high), HL / LL / EL (higher / lower / equal low),
    equal = |price / previous - 1| <= equal_tolerance_pct. The first pivot of a kind has no label.
"""

import numpy as np
import pandas as pd

from app.price_action.config import PriceActionConfig

SWING_COLUMNS = (
    "swing_id",
    "kind",
    "pivot_idx",
    "pivot_ts",
    "price",
    "label",
    "change_pct",
    "confirmed_idx",
    "confirmed_at",
)


def _pivots(values: pd.Series, k_left: int, k_right: int, high: bool) -> np.ndarray:
    left = values.shift(1).rolling(k_left, min_periods=k_left)
    right = values.shift(-k_right).rolling(k_right, min_periods=k_right)
    if high:
        mask = (values > left.max()) & (values >= right.max())
    else:
        mask = (values < left.min()) & (values <= right.min())
    return np.flatnonzero(mask.fillna(False).to_numpy(dtype=bool))


def _label(kind: str, change: float, tol: float) -> str:
    if kind == "high":
        return "HH" if change > tol else "LH" if change < -tol else "EH"
    return "HL" if change > tol else "LL" if change < -tol else "EL"


def detect_swings(bars: pd.DataFrame, cfg: PriceActionConfig) -> pd.DataFrame:
    """All CONFIRMED pivots in `bars`, sorted by (confirmed_idx, pivot_idx, kind)."""
    index = bars.index
    rows: list[dict[str, object]] = []
    for kind, col in (("high", "high"), ("low", "low")):
        values = bars[col].astype("float64")
        previous: float | None = None
        for i in _pivots(values, cfg.swing_left_bars, cfg.swing_right_bars, kind == "high"):
            price = float(values.iloc[i])
            change = None if previous is None else round(price / previous - 1, 10)
            rows.append(
                {
                    "kind": kind,
                    "pivot_idx": int(i),
                    "pivot_ts": index[i],
                    "price": price,
                    "label": None
                    if change is None
                    else _label(kind, change, cfg.equal_tolerance_pct),
                    "change_pct": change,
                    "confirmed_idx": int(i) + cfg.swing_right_bars,
                    "confirmed_at": index[int(i) + cfg.swing_right_bars],
                }
            )
            previous = price
    swings = pd.DataFrame(rows, columns=list(SWING_COLUMNS[1:]))
    swings = swings.sort_values(["confirmed_idx", "pivot_idx", "kind"], kind="stable")
    swings.insert(0, "swing_id", range(len(swings)))
    swings["label"] = swings["label"].astype("string")  # <NA> for the first pivot of a kind
    swings["change_pct"] = swings["change_pct"].astype("float64")
    return swings.reset_index(drop=True)
