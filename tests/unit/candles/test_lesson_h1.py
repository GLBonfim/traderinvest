import pandas as pd

from app.candles.lesson_h1 import analyze_h1, permits


def setup_bars():
    bars = pd.DataFrame(
        {"open": 110.0, "high": 112.0, "low": 96.0, "close": 100.0},
        index=pd.date_range("2023-01-03", periods=16, freq="h", tz="UTC"),
    )
    bars.iloc[12] = [110.0, 113.0, 103.0, 110.0]
    bars.iloc[13] = [108.0, 109.0, 93.0, 95.0]
    bars.iloc[14] = [96.0, 102.0, 94.0, 100.0]
    bars.iloc[15] = [106.0, 107.0, 100.0, 104.0]
    return bars


def test_ordered_sequence_and_fixed_past_anchors():
    lesson = analyze_h1(setup_bars())
    assert lesson["ready"] and lesson["direction"] == -1
    assert lesson["sequence"]["expansion_low"] == 93
    assert lesson["sequence"]["expansion_high"] == 113
    assert permits({"lesson_h1": lesson}, -1, True)
    assert not permits({"lesson_h1": lesson}, 1, True)
    assert not permits({"lesson_h1": lesson}, -1, False)


def test_sweep_without_later_structure_is_rejected():
    bars = setup_bars()
    bars.iloc[13] = [108.0, 109.0, 97.0, 100.0]
    assert not analyze_h1(bars)["ready"]


def test_mss_before_sweep_cannot_qualify():
    bars = setup_bars()
    bars.iloc[[12, 13]] = bars.iloc[[13, 12]].to_numpy()
    assert not analyze_h1(bars)["ready"]


def test_missing_hour_and_stale_or_missing_metadata_fail_closed():
    assert not analyze_h1(setup_bars().drop(setup_bars().index[10]))["ready"]
    assert not permits({}, -1, True)
    assert not permits({"lesson_h1": {"version": "old", "ready": True}}, -1, True)


def test_no_ote_retest_no_proposal():
    bars = setup_bars()
    bars.iloc[-1] = [101.0, 103.0, 99.0, 100.0]
    assert not analyze_h1(bars)["ready"]


def test_new_extreme_invalidates_fixed_expansion():
    bars = setup_bars()
    bars.iloc[14] = [96.0, 102.0, 90.0, 100.0]
    assert not analyze_h1(bars)["ready"]


def test_original_prefix_is_independent_of_future_prices():
    bars = setup_bars()
    expected = analyze_h1(bars)
    future = pd.DataFrame(
        {"open": [104.0], "high": [200.0], "low": [1.0], "close": [190.0]},
        index=[bars.index[-1] + pd.Timedelta(hours=1)],
    )
    combined = pd.concat([bars, future])
    assert analyze_h1(combined.iloc[:16]) == expected
