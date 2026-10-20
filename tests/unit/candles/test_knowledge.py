"""Synthetic recognition examples, counterexamples and causal information boundaries."""

import pandas as pd
import pytest

from app.candles.knowledge import CATALOG, explain_pattern, recognize
from app.candles.patterns import REGISTRY
from tests.unit.candles.helpers import bars, trend_prefix

CASES = {
    "bullish_harami": [(10, 10.5, 5.5, 6), (7, 9.5, 6.1, 8)],
    "bearish_harami": [(6, 10.5, 5.5, 10), (9, 9.5, 6.1, 8)],
    "bullish_harami_cross": [(10, 10.5, 5.5, 6), (8, 9, 7, 8.05)],
    "bearish_harami_cross": [(6, 10.5, 5.5, 10), (8, 9, 7, 8.05)],
    "piercing_line": [(10, 10.5, 5.5, 6), (5, 9.5, 4.8, 9)],
    "dark_cloud_cover": [(6, 10.5, 5.5, 10), (11, 11.2, 6.5, 7)],
    "dragonfly_doji": [(10, 10.1, 7, 10.02)],
    "gravestone_doji": [(10, 13, 9.9, 10.02)],
    "morning_doji_star": [(10, 10.5, 5.5, 6), (5.8, 6, 5, 5.8), (6, 9.5, 5.7, 9)],
    "evening_doji_star": [(6, 10.5, 5.5, 10), (10.2, 11, 10, 10.2), (10, 10.3, 6.5, 7)],
}


def frame(name: str) -> pd.DataFrame:
    trend = "down" if CATALOG[name]["prior_trend"] == "down" else "up"
    data = bars(trend_prefix(trend, 11 if trend == "down" else 20, 25) + CASES[name])
    data.index = pd.date_range("2024-01-02", periods=len(data), freq="h", tz="UTC")
    return data


def scan(data: pd.DataFrame, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    return recognize(data, timeframe="H1", as_of=as_of or data.index[-1] + pd.Timedelta(hours=1))


def test_thirty_lessons_and_original_execution_registry_frozen() -> None:
    assert len(CATALOG) == 30
    assert sum(x["live_detector"] for x in CATALOG.values()) == 20
    assert len(REGISTRY) == 20
    for name in CATALOG:
        assert explain_pattern(name)["source"].startswith("https://")
        assert CATALOG[name]["statistically_approved"] is False


@pytest.mark.parametrize("name", CASES)
def test_ten_additional_patterns_have_geometric_examples(name: str) -> None:
    data = frame(name)
    obs = scan(data)
    hit = obs[(obs.ts == data.index[-1]) & (obs.pattern == name)]
    assert len(hit) == 1
    assert not hit.iloc[0].order_authorized
    assert not hit.iloc[0].live_detector
    assert hit.iloc[0].strength_is_probability == False  # noqa: E712
    assert bool(hit.iloc[0].context_requirements_met)


@pytest.mark.parametrize("name", CASES)
def test_invalid_ohlc_does_not_form_research_pattern(name: str) -> None:
    data = frame(name)
    data.iloc[-1, data.columns.get_loc("high")] = 0
    obs = scan(data)
    assert obs.empty or not ((obs.ts == data.index[-1]) & (obs.pattern == name)).any()


def test_confirmation_waits_for_closed_next_bar_and_is_not_future_leakage() -> None:
    data = frame("bullish_harami")
    ts = data.index[-1]
    before = scan(data)
    row = before[(before.ts == ts) & (before.pattern == "bullish_harami")].iloc[0]
    assert row.confirmation == "pending"
    data.loc[ts + pd.Timedelta(hours=1)] = [9, 11, 6, 10.8, 0]
    pd.testing.assert_frame_equal(before, scan(data, ts + pd.Timedelta(hours=1)))
    after = scan(data)
    row = after[(after.ts == ts) & (after.pattern == "bullish_harami")].iloc[0]
    assert row.confirmation == "confirmed"
    assert row.confirmation_known_at == ts + pd.Timedelta(hours=2)


def test_both_boundaries_crossed_are_ambiguous_not_a_clean_confirmation() -> None:
    data = frame("bullish_harami")
    ts = data.index[-1]
    data.loc[ts + pd.Timedelta(hours=1)] = [9, 11, 5, 10.8, 0]
    obs = scan(data)
    assert (
        obs[(obs.ts == ts) & (obs.pattern == "bullish_harami")].iloc[0].confirmation == "ambiguous"
    )


def test_no_pattern_across_missing_bar_or_no_prior_context() -> None:
    data = frame("bullish_harami")
    data.index = data.index[:-1].append(pd.DatetimeIndex([data.index[-1] + pd.Timedelta(hours=2)]))
    obs = scan(data)
    assert not ((obs.ts == data.index[-1]) & (obs.pattern == "bullish_harami")).any()
    data = frame("bullish_harami").iloc[-2:]
    obs = scan(data)
    assert not bool(obs[obs.pattern == "bullish_harami"].iloc[0].context_requirements_met)


def test_doji_variants_remain_neutral_and_never_authorize_an_order() -> None:
    obs = scan(frame("dragonfly_doji"))
    subset = obs[obs.pattern.isin(["doji", "dragonfly_doji"])]
    assert set(subset.orientation) == {"neutral"}
    assert set(subset.confirmation) == {"not_directional"}
    assert not subset.order_authorized.any()


def test_piercing_requires_actual_price_gap_not_a_relaxed_forex_label() -> None:
    data = frame("piercing_line")
    data.iloc[-1, data.columns.get_loc("open")] = 6
    obs = scan(data)
    assert not ((obs.ts == data.index[-1]) & (obs.pattern == "piercing_line")).any()


def test_support_and_resistance_use_only_bars_before_pattern() -> None:
    data = frame("bullish_harami")
    obs = scan(data)
    hit = obs[(obs.ts == data.index[-1]) & (obs.pattern == "bullish_harami")].iloc[0]
    prior = data.iloc[-22:-2]
    assert hit.prior_support_20 == prior.low.min()
    assert hit.prior_resistance_20 == prior.high.max()


def test_incomplete_bar_flag_is_excluded() -> None:
    data = frame("bullish_harami")
    data["is_closed"] = True
    data.iloc[-1, data.columns.get_loc("is_closed")] = False
    obs = scan(data)
    assert obs.empty or not (obs.ts == data.index[-1]).any()


def test_real_data_loader_requires_twelve_m5_bars_and_correct_hash(tmp_path) -> None:
    import hashlib

    from app.fx.research.candle_knowledge import development_h1

    folder = tmp_path / "EURUSD"
    folder.mkdir()
    path = folder / "BID_M5_test.parquet"
    data = pd.DataFrame(
        {
            "ts": pd.date_range("2023-01-03", periods=23, freq="5min", tz="UTC"),
            "open": 1.1,
            "high": 1.2,
            "low": 1.0,
            "close": 1.15,
        }
    )
    data.to_parquet(path)
    chunk = {
        "pair": "EURUSD",
        "side": "BID",
        "first": str(data.ts.min()),
        "last": str(data.ts.max()),
        "file": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    h1, provenance = development_h1(tmp_path, "EURUSD", [chunk])
    assert len(h1) == 1 and h1.index[0].hour == 0
    assert len(provenance) == 1 and h1.volume.iloc[0] == 0
    chunk["sha256"] = "wrong"
    with pytest.raises(ValueError, match="hash mismatch"):
        development_h1(tmp_path, "EURUSD", [chunk])
