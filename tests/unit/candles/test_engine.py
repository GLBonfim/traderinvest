"""Engine-level guarantees: point-in-time (no look-ahead), determinism, config, output schema,
and a regression snapshot on real SPY bars."""

import dataclasses

import numpy as np
import pandas as pd
import pytest

from app.candles.config import ENGINE_VERSION, CandleConfig
from app.candles.engine import OBSERVATION_COLUMNS, CandlestickEngine
from tests.fixtures.spy_daily_sample import SPY_2020_Q1
from tests.unit.candles.helpers import bars


def random_walk(n: int, seed: int) -> pd.DataFrame:
    """Seeded synthetic OHLCV with realistic geometry (not market data)."""
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    open_ = np.r_[100.0, close[:-1]] * (1 + rng.normal(0, 0.003, n))
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    candles = list(zip(open_, hi, lo, close, strict=True))
    return bars(candles, volumes=rng.integers(5e5, 2e6, n).astype(float))


def spy_sample() -> pd.DataFrame:
    df = pd.DataFrame(SPY_2020_Q1, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts").astype("float64")


ENGINE = CandlestickEngine()


def obs_at(observations: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
    return observations[observations["ts"] == ts].reset_index(drop=True)


# ── point-in-time / leakage ──


@pytest.mark.parametrize("frame_factory", [lambda: random_walk(160, seed=7), spy_sample])
def test_truncated_history_gives_identical_output_at_every_bar(frame_factory) -> None:  # type: ignore[no-untyped-def]
    """features(T) computed with data up to T == features(T) computed with ALL data."""
    full_bars = frame_factory()
    full = ENGINE.analyze(full_bars)
    for i in range(len(full_bars)):
        ts = full_bars.index[i]
        partial = ENGINE.analyze(full_bars.iloc[: i + 1])
        pd.testing.assert_series_equal(partial.geometry.iloc[-1], full.geometry.iloc[i])
        pd.testing.assert_frame_equal(
            obs_at(partial.observations, ts), obs_at(full.observations, ts), check_dtype=False
        )


def test_truncation_check_detects_a_deliberately_leaky_feature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: the harness above must FAIL if a feature peeks at t+1."""
    import app.candles.engine as engine_module
    from app.candles.geometry import compute_geometry

    def leaky_geometry(b: pd.DataFrame, cfg: CandleConfig) -> pd.DataFrame:
        g = compute_geometry(b, cfg)
        g["body_ratio"] = g["body_ratio"].shift(-1)  # uses the NEXT bar
        return g

    monkeypatch.setattr(engine_module, "compute_geometry", leaky_geometry)
    b = random_walk(40, seed=2)
    full = CandlestickEngine().analyze(b)
    partial = CandlestickEngine().analyze(b.iloc[:20])
    with pytest.raises(AssertionError):
        pd.testing.assert_series_equal(partial.geometry.iloc[-1], full.geometry.iloc[19])


def test_altering_future_bars_never_changes_past_observations() -> None:
    base = random_walk(200, seed=11)
    cut = 120
    reference = ENGINE.analyze(base)
    rng = np.random.default_rng(99)
    for _ in range(5):
        altered = base.copy()
        future = altered.iloc[cut + 1 :]
        scale = rng.uniform(0.5, 1.5, size=(len(future), 1))
        altered.iloc[cut + 1 :, :4] = future.iloc[:, :4].to_numpy() * scale
        altered["high"] = altered[["open", "high", "low", "close"]].max(axis=1)
        altered["low"] = altered[["open", "high", "low", "close"]].min(axis=1)
        altered.iloc[cut + 1 :, 4] = rng.integers(1, 10**7, len(future))
        result = ENGINE.analyze(altered)
        past = result.observations[result.observations["ts"] <= base.index[cut]]
        ref_past = reference.observations[reference.observations["ts"] <= base.index[cut]]
        pd.testing.assert_frame_equal(past, ref_past)
        pd.testing.assert_frame_equal(
            result.geometry.iloc[: cut + 1], reference.geometry.iloc[: cut + 1]
        )


def test_observation_window_never_extends_past_its_timestamp() -> None:
    obs = ENGINE.analyze(random_walk(300, seed=3)).observations
    assert (obs["first_bar_ts"] <= obs["ts"]).all()
    span = (obs["ts"] - obs["first_bar_ts"]).dt.days
    assert (span == obs["bars_in_pattern"] - 1).all()  # daily synthetic index


# ── determinism / config ──


def test_output_is_deterministic() -> None:
    b = random_walk(300, seed=5)
    a1, a2 = ENGINE.analyze(b), CandlestickEngine().analyze(b.copy())
    pd.testing.assert_frame_equal(a1.observations, a2.observations)
    pd.testing.assert_frame_equal(a1.geometry, a2.geometry)
    assert a1.config_fingerprint == a2.config_fingerprint
    assert a1.engine_version == ENGINE_VERSION


def test_observations_are_sorted_by_ts_then_pattern() -> None:
    obs = ENGINE.analyze(random_walk(300, seed=8)).observations
    assert list(obs.index) == list(range(len(obs)))
    keys = list(zip(obs["ts"], obs["pattern"], strict=True))
    assert keys == sorted(keys)


def test_fingerprint_changes_with_any_threshold() -> None:
    base = CandleConfig()
    assert base.fingerprint() == CandleConfig().fingerprint()
    changed = dataclasses.replace(base, doji_max_body_ratio=0.05)
    assert changed.fingerprint() != base.fingerprint()


@pytest.mark.parametrize(
    "overrides",
    [
        {"doji_max_body_ratio": 1.5},
        {"trend_lookback": 0},
        {"long_body_min_multiple": 0},
        {"doji_max_body_ratio": 0.4},  # must stay below the spinning-top bound
        {"small_body_max_ratio": 0.6},  # must stay below large_body_min_ratio
        {"engulfing_ideal_body_multiple": 1.0},  # log scale needs ideal > 1
    ],
)
def test_invalid_config_is_rejected(overrides: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        CandleConfig(**overrides)  # type: ignore[arg-type]


def test_thresholds_change_classification() -> None:
    candle = bars([(100, 101, 99, 100.3)])  # body ratio 0.15
    assert "doji" not in set(ENGINE.analyze(candle).observations["pattern"])
    loose = CandlestickEngine(CandleConfig(doji_max_body_ratio=0.2))
    assert "doji" in set(loose.analyze(candle).observations["pattern"])


# ── output contract: observations, never decisions ──


def test_observation_schema_contains_no_trading_fields() -> None:
    forbidden = ("signal", "buy", "sell", "entry", "stop", "target", "position", "expected", "pnl")
    assert not [c for c in OBSERVATION_COLUMNS if any(f in c for f in forbidden)]
    obs = ENGINE.analyze(random_walk(300, seed=1)).observations
    assert tuple(obs.columns) == OBSERVATION_COLUMNS
    assert set(obs["orientation"]) <= {"bullish", "bearish", "neutral"}


def test_typed_observations_roundtrip() -> None:
    a = ENGINE.analyze(spy_sample(), instrument_id=7, timeframe="1d")
    typed = a.to_observations()
    assert len(typed) == len(a.observations)
    assert all(o.instrument_id == 7 and o.timeframe == "1d" for o in typed)
    assert all(o.ts.tzinfo is not None for o in typed)


def test_empty_input() -> None:
    a = ENGINE.analyze(bars([]))
    assert a.observations.empty and a.geometry.empty


# ── regression on real SPY bars ──

# Snapshot of engine v1.0.0 with default thresholds on SPY 2020-01-27..2020-04-24.
# A change here means detector behaviour changed: bump ENGINE_VERSION and document it.
EXPECTED_SPY_2020 = (
    ("2020-02-03", "inside_bar"), ("2020-02-06", "doji"), ("2020-02-07", "spinning_top"),
    ("2020-02-10", "bullish_engulfing"), ("2020-02-10", "marubozu"), ("2020-02-10", "outside_bar"),
    ("2020-02-13", "outside_bar"), ("2020-02-14", "doji"), ("2020-02-14", "hanging_man"),
    ("2020-02-14", "inside_bar"), ("2020-02-18", "doji"), ("2020-02-24", "doji"),
    ("2020-02-25", "long_body"), ("2020-02-28", "long_body"), ("2020-03-02", "long_body"),
    ("2020-03-04", "inside_bar"), ("2020-03-04", "long_body"), ("2020-03-05", "spinning_top"),
    ("2020-03-09", "doji"), ("2020-03-09", "inverted_hammer"), ("2020-03-10", "hammer"),
    ("2020-03-10", "tweezer_bottom"), ("2020-03-13", "hammer"), ("2020-03-13", "tweezer_bottom"),
    ("2020-03-16", "doji"), ("2020-03-17", "tweezer_bottom"), ("2020-03-18", "spinning_top"),
    ("2020-03-19", "doji"), ("2020-03-19", "inside_bar"), ("2020-03-20", "long_body"),
    ("2020-03-25", "spinning_top"), ("2020-03-26", "long_body"), ("2020-03-27", "doji"),
    ("2020-03-27", "inside_bar"), ("2020-04-02", "bullish_engulfing"), ("2020-04-02", "inside_bar"),
    ("2020-04-07", "long_body"), ("2020-04-08", "tweezer_bottom"), ("2020-04-09", "spinning_top"),
    ("2020-04-13", "hanging_man"), ("2020-04-15", "doji"), ("2020-04-16", "doji"),
    ("2020-04-16", "inside_bar"), ("2020-04-20", "shooting_star"), ("2020-04-22", "spinning_top"),
    ("2020-04-23", "shooting_star"), ("2020-04-24", "tweezer_bottom"),
)  # fmt: skip


def test_spy_regression_snapshot() -> None:
    obs = ENGINE.analyze(spy_sample()).observations
    got = tuple((t.strftime("%Y-%m-%d"), p) for t, p in zip(obs["ts"], obs["pattern"], strict=True))
    assert got == EXPECTED_SPY_2020


def test_spy_geometry_matches_hand_calculation() -> None:
    b = spy_sample()
    g = ENGINE.analyze(b).geometry
    day = "2020-03-16"
    o, h, lo, c = (b.loc[day, col].item() for col in ("open", "high", "low", "close"))
    prev_close = b["close"].shift(1).loc[day].item()
    assert g.loc[day, "range"].item() == pytest.approx(h - lo)
    assert g.loc[day, "body_ratio"].item() == pytest.approx(abs(c - o) / (h - lo))
    # Same gap that Phase 2 validation flagged as gap_outlier (-10.45%).
    assert g.loc[day, "gap"].item() == pytest.approx(o / prev_close - 1)
    assert round(g.loc[day, "gap"].item() * 100, 2) == -10.45
