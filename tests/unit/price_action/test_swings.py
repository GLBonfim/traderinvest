import dataclasses

import pandas as pd

from app.price_action.swings import detect_swings
from tests.unit.price_action.helpers import FAST, hl_bars


def highs(swings: pd.DataFrame) -> pd.DataFrame:
    return swings[swings["kind"] == "high"].reset_index(drop=True)


def test_obvious_swing_high_and_low() -> None:
    b = hl_bars([1, 2, 5, 2, 1, 2, 3], lows=[0.5, 1.5, 4.5, 1.5, 0.2, 1.5, 2.5])
    s = detect_swings(b, FAST)
    hi = highs(s)
    assert hi["pivot_idx"].tolist() == [2]
    assert hi["price"].tolist() == [5.0]
    lo = s[s["kind"] == "low"]
    assert lo["pivot_idx"].tolist() == [4]


def test_confirmation_latency() -> None:
    b = hl_bars([1, 2, 5, 2, 1, 1.5, 1.2])
    s = highs(detect_swings(b, FAST))
    assert s.loc[0, "pivot_ts"] == b.index[2]
    assert s.loc[0, "confirmed_idx"] == 4
    assert s.loc[0, "confirmed_at"] == b.index[4]  # known only after 2 more bars closed
    # With data only up to bar 3 the pivot does not exist yet.
    assert highs(detect_swings(b.iloc[:4], FAST)).empty
    assert len(highs(detect_swings(b.iloc[:5], FAST))) == 1


def test_insufficient_history_on_either_side() -> None:
    # Peak at bar 1 has only one bar on the left; peak at the last bar has no right bars.
    s = highs(detect_swings(hl_bars([1, 5, 2, 1, 0.5, 3, 6]), FAST))
    assert s.empty


def test_first_of_equal_highs_wins() -> None:
    s = highs(detect_swings(hl_bars([1, 2, 5, 5, 2, 1, 0.5]), FAST))
    assert s["pivot_idx"].tolist() == [2]  # right side >= allows equality; left side strict


def test_left_side_equality_is_rejected() -> None:
    s = highs(detect_swings(hl_bars([1, 5, 5, 2, 1, 0.5]), FAST))
    assert 2 not in s["pivot_idx"].tolist()


def test_labels_and_equal_tolerance_boundary() -> None:
    # peaks: 100, 100.1 (+0.1% -> exactly tolerance -> EH), 101 (HH), 100 (LH)
    peaks = [100, 100.1, 101, 100]
    seq: list[float] = []
    for p in peaks:
        seq += [p - 3, p - 2, p, p - 2, p - 3]
    s = highs(detect_swings(hl_bars(seq), FAST))
    assert s["label"].tolist()[0] is None or pd.isna(s["label"].tolist()[0])
    assert s["label"].tolist()[1:] == ["EH", "HH", "LH"]
    assert s.loc[1, "change_pct"] == 0.001


def test_low_labels() -> None:
    troughs = [100, 101, 99, 99.05]
    seq: list[float] = []
    for p in troughs:
        seq += [p + 3, p + 2, p, p + 2, p + 3]
    b = hl_bars([x + 1 for x in seq], lows=seq)
    lo = detect_swings(b, FAST)
    assert lo[lo["kind"] == "low"]["label"].tolist()[1:] == ["HL", "LL", "EL"]


def test_wider_pivot_window_changes_latency() -> None:
    cfg = dataclasses.replace(FAST, swing_left_bars=3, swing_right_bars=3)
    s = highs(detect_swings(hl_bars([1, 2, 3, 9, 3, 2, 1, 0.5]), cfg))
    assert s.loc[0, "confirmed_idx"] - s.loc[0, "pivot_idx"] == 3
