import pandas as pd

from app.fx.research.m5_session_training import session_patterns


def setup():
    idx = pd.date_range("2023-01-03", periods=104, freq="5min", tz="UTC")
    df = pd.DataFrame({"open": 1.09, "high": 1.10, "low": 1.00, "close": 1.05}, index=idx)
    df.iloc[93:96] = [1.06, 1.07, 1.04, 1.05]
    df.iloc[96] = [1.09, 1.11, 1.06, 1.08]
    df.iloc[97] = [1.08, 1.09, 1.02, 1.03]
    df.iloc[98] = [1.035, 1.05, 1.03, 1.04]
    df.iloc[99] = [1.045, 1.055, 1.035, 1.04]
    df.iloc[100] = [1.075, 1.08, 1.05, 1.06]
    df.iloc[101:] = [1.075, 1.08, 1.05, 1.06]
    return df


def test_full_causal_session_sequence_emits_first_ote_only():
    df = setup()
    p = session_patterns(df)
    assert p.reversal_bear.iloc[100] >= 0.5
    assert p.reversal_bear.iloc[:100].sum() == 0
    assert p.reversal_bear.iloc[101:].sum() == 0
    assert p.filter(like="_bull").to_numpy().sum() == 0


def test_future_changes_do_not_change_earlier_session_events():
    df = setup()
    before = session_patterns(df.iloc[:101])
    df.iloc[101:] *= 2
    pd.testing.assert_frame_equal(before, session_patterns(df).iloc[:101])


def test_missing_reference_bar_and_missing_mss_fail_closed():
    df = setup()
    assert session_patterns(df.drop(df.index[20])).reversal_bear.sum() == 0
    df.iloc[97] = [1.08, 1.09, 1.05, 1.07]
    assert session_patterns(df).reversal_bear.sum() == 0


def test_pivot_requires_two_closed_bars_and_locked_anchor_never_redraws():
    df = setup()
    df.iloc[98] = [1.075, 1.08, 1.03, 1.06]
    assert session_patterns(df).reversal_bear.iloc[98] == 0
    df = setup()
    df.iloc[100] = [1.075, 1.08, 1.01, 1.06]
    assert session_patterns(df).reversal_bear.sum() == 0
