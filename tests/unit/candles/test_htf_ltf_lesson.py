import pandas as pd
import pytest

from app.candles.htf_ltf_lesson import curriculum, observe, ote_zone


def sample():
    index = pd.date_range("2023-01-03", periods=51, freq="5min", tz="UTC")
    return pd.DataFrame({"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0}, index=index)


def test_reference_and_sweep_are_not_orders():
    bars = sample()
    bars.iloc[-1] = [10.0, 12.0, 9.5, 10.5]
    out = observe(bars, as_of=bars.index[-1] + pd.Timedelta(minutes=5))
    sweep = next(f for f in out["facts"] if f["kind"] == "crt_upper_sweep_return")
    assert sweep["direction"] == "bearish"
    assert not sweep["setup_complete"] and not sweep["mss_confirmed"]
    assert out["reference"]["high"] == 11
    assert out["reference"]["low"] == 9
    assert out["execution_enabled"] is False


def test_future_and_unclosed_bars_cannot_change_observation():
    bars = sample()
    now = bars.index[-2] + pd.Timedelta(minutes=5)
    expected = observe(bars.iloc[:-1], as_of=now)
    bars.iloc[-1] = [10.0, 100.0, 1.0, 90.0]
    assert observe(bars, as_of=now) == expected


def test_missing_reference_and_ltf_are_explicit():
    bars = sample().drop(sample().index[20])
    assert (
        "complete_previous_h4"
        in observe(bars, as_of=bars.index[-1] + pd.Timedelta(minutes=5))["missing"]
    )
    bars = sample().drop(sample().index[-3])
    assert (
        "contiguous_ltf" in observe(bars, as_of=bars.index[-1] + pd.Timedelta(minutes=5))["missing"]
    )


def test_breakout_without_return_is_not_sweep():
    bars = sample()
    bars.iloc[-1] = [10.0, 12.0, 9.5, 11.5]
    out = observe(bars, as_of=bars.index[-1] + pd.Timedelta(minutes=5))
    assert not any(f["kind"].startswith("crt_") for f in out["facts"])


def test_fvg_is_geometry_only():
    bars = sample()
    bars.iloc[-1] = [12.0, 13.0, 11.5, 12.5]
    out = observe(bars, as_of=bars.index[-1] + pd.Timedelta(minutes=5))
    assert any(f["kind"] == "bullish_fvg_geometry" for f in out["facts"])
    assert out["statistically_approved"] is False


def test_ote_levels_and_direction():
    assert ote_zone(100, 200, "bullish") == pytest.approx((121, 138))
    assert ote_zone(100, 200, "bearish") == pytest.approx((162, 179))
    with pytest.raises(ValueError):
        ote_zone(200, 100, "bullish")


@pytest.mark.parametrize("role", ["analista", "reversao", "continuacao", "impulso"])
def test_curriculum_has_sources_and_no_approval(role):
    out = curriculum(role)
    assert out["lessons"] and all(x["pages"] for x in out["lessons"].values())
    assert not out["execution_enabled"] and not out["training_weights_changed"]


def test_bad_data_rejected():
    bars = sample()
    bars.iloc[-1, 1] = float("nan")
    with pytest.raises(ValueError):
        observe(bars, as_of=bars.index[-1] + pd.Timedelta(minutes=5))


def test_bullish_sweep_stays_bullish_not_sell():
    bars = sample()
    bars.iloc[-1] = [10.0, 10.5, 8.0, 9.5]
    out = observe(bars, as_of=bars.index[-1] + pd.Timedelta(minutes=5))
    fact = next(x for x in out["facts"] if x["kind"] == "crt_lower_sweep_return")
    assert fact["direction"] == "bullish" and not fact["setup_complete"]


def test_unconfirmed_pivot_is_not_structure_break():
    bars = sample()
    # High at penultimate bar has no two right bars at the decision time.
    bars.iloc[-2] = [10.0, 12.0, 9.0, 10.0]
    bars.iloc[-1] = [10.0, 13.0, 9.0, 12.5]
    out = observe(bars, as_of=bars.index[-1] + pd.Timedelta(minutes=5))
    assert not any(x["kind"] == "bullish_structure_break" for x in out["facts"])


def test_confirmed_pivot_break_is_not_automatically_mss():
    bars = sample()
    bars.iloc[-5] = [10.0, 12.0, 9.0, 10.0]
    bars.iloc[-1] = [10.0, 13.0, 9.0, 12.5]
    out = observe(bars, as_of=bars.index[-1] + pd.Timedelta(minutes=5))
    fact = next(x for x in out["facts"] if x["kind"] == "bullish_structure_break")
    assert fact["classification"] == "MSS_vs_BOS_requires_prior_trend"
