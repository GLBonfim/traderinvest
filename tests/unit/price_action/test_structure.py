import pytest

from app.price_action.engine import PriceActionEngine, classify_structure
from tests.unit.price_action.helpers import FAST, zigzag


@pytest.mark.parametrize(
    ("highs", "lows", "expected"),
    [
        (["HH", "HH"], ["HL", "HL"], "uptrend"),
        (["LH", "LH"], ["LL", "LL"], "downtrend"),
        (["LH", "EH"], ["HL", "EL"], "range"),
        (["EH", "EH"], ["EL", "EL"], "range"),
        (["HH", "HH"], ["HL", "LL"], "transition"),
        (["HH", "LH"], ["HL", "HL"], "transition"),
        (["HH", "HH"], ["LL", "LL"], "transition"),  # broadening: not a trend, not a range
        (["HH"], ["HL", "HL"], "insufficient_data"),
        ([], [], "insufficient_data"),
    ],
)
def test_structure_rules(highs: list[str], lows: list[str], expected: str) -> None:
    assert classify_structure(highs, lows, 2) == expected


def test_only_last_n_labels_matter() -> None:
    assert classify_structure(["LH", "LH", "HH", "HH"], ["LL", "HL", "HL"], 2) == "uptrend"


def run(points: list[float]):  # type: ignore[no-untyped-def]
    return PriceActionEngine(FAST).analyze(zigzag(points))


def test_rising_zigzag_is_uptrend_with_full_quality() -> None:
    a = run([100, 110, 104, 114, 108, 118, 112, 122, 116])
    last = a.state.iloc[-1]
    assert last["structure"] == "uptrend" and last["trend"] == "up"
    assert last["trend_quality"] == 1.0
    assert set(last["trend_evidence"].split(",")) == {"HH", "HL"}


def test_falling_zigzag_is_downtrend() -> None:
    last = run([122, 112, 118, 108, 114, 104, 110, 100, 106]).state.iloc[-1]
    assert (last["structure"], last["trend"]) == ("downtrend", "down")


def test_contracting_zigzag_is_range() -> None:
    last = run([100, 120, 102, 118, 104, 116, 106, 114, 108]).state.iloc[-1]
    assert (last["structure"], last["trend"]) == ("range", "sideways")


def test_mixed_structure_is_transition() -> None:
    last = run([100, 110, 104, 114, 108, 112, 100, 116, 95]).state.iloc[-1]
    assert last["structure"] == "transition" and last["trend"] == "unclear"
    assert last["trend_quality"] != last["trend_quality"]  # NaN for unclear trends


def test_short_history_is_insufficient() -> None:
    assert set(run([100, 110, 104]).state["structure"]) == {"insufficient_data"}


def test_structure_changes_only_when_pivot_is_confirmed() -> None:
    a = run([100, 110, 104, 114, 108, 118, 112, 122, 116])
    labeled = a.swings.dropna(subset=["label"]).sort_values("swing_id")
    assert (labeled["pivot_idx"] < labeled["confirmed_idx"]).all()
    for t in range(len(a.state)):
        known = labeled[labeled["confirmed_idx"] <= t]["label"].tolist()
        expected = ",".join(known[-FAST.trend_quality_labels :])
        assert a.state.iloc[t]["trend_evidence"] == expected, t
