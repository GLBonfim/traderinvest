"""Engine-level guarantees: warm-up, input validation, numerical edge cases, point-in-time
(with a leaky control), determinism, configuration, output contract, SPY regression."""

import dataclasses

import numpy as np
import pandas as pd
import pytest

from app.candles.engine import CandlestickEngine
from app.indicators.config import ENGINE_VERSION, IndicatorConfig
from app.indicators.engine import IndicatorAnalysis, IndicatorEngine
from tests.unit.candles.helpers import bars
from tests.unit.candles.test_engine import random_walk, spy_sample

ENGINE = IndicatorEngine()
SMALL = IndicatorConfig(
    sma_periods=(3, 5),
    ema_periods=(3, 5),
    macd_fast=3,
    macd_slow=6,
    macd_signal=4,
    rsi_period=4,
    stoch_period=4,
    roc_period=3,
    atr_period=4,
    bollinger_period=5,
    realized_vol_period=5,
    relative_volume_period=4,
)


def indicator_frame(a: IndicatorAnalysis) -> pd.DataFrame:
    return a.values[a.indicator_columns]


# ── warm-up ──


@pytest.mark.parametrize("cfg", [IndicatorConfig(), SMALL])
def test_warmup_boundaries_are_exact_for_every_column(cfg: IndicatorConfig) -> None:
    a = IndicatorEngine(cfg).analyze(random_walk(400, seed=42))
    for row in a.catalog.itertuples():
        col = a.values[row.column]
        warm = int(row.first_valid_index)
        assert col.iloc[:warm].isna().all(), f"{row.column} has values before warm-up"
        assert col.iloc[warm:].notna().all(), f"{row.column} has NaN after warm-up"


def test_warmup_never_filled_with_zero() -> None:
    a = ENGINE.analyze(random_walk(30, seed=1))
    assert a.values["sma_200"].isna().all()
    assert a.values["ema_200"].isna().all()
    assert a.values["realized_vol_20"].iloc[:20].isna().all()


# ── input validation ──


def test_input_validation() -> None:
    b = random_walk(40, seed=2)
    with pytest.raises(ValueError, match="UTC"):
        ENGINE.analyze(b.tz_convert("America/New_York"))
    with pytest.raises(ValueError, match="strictly increasing"):
        ENGINE.analyze(b.iloc[::-1])
    with pytest.raises(ValueError, match="closed"):
        ENGINE.analyze(b.assign(is_closed=[True] * 39 + [False]))
    for col, value, msg in (
        ("close", np.nan, "missing or infinite"),
        ("high", np.inf, "missing or infinite"),
        ("low", 0.0, "non-positive"),
        ("volume", -1.0, "volume"),
        ("volume", np.inf, "volume"),
    ):
        bad = b.copy()
        bad.iloc[10, bad.columns.get_loc(col)] = value
        with pytest.raises(ValueError, match=msg):
            ENGINE.analyze(bad)
    with pytest.raises(ValueError, match="missing columns"):
        ENGINE.analyze(b.drop(columns=["volume"]))


# ── volume behaviour ──


def test_missing_volume_only_affects_volume_indicators() -> None:
    b = random_walk(80, seed=3)
    full = ENGINE.analyze(b).values
    no_vol = ENGINE.analyze(b.assign(volume=np.nan)).values
    assert no_vol["relative_volume_20"].isna().all()
    assert no_vol["obv"].isna().all()
    price_cols = [c for c in full.columns if c not in ("relative_volume_20", "obv")]
    pd.testing.assert_frame_equal(no_vol[price_cols], full[price_cols])


def test_zero_volume_bars() -> None:
    b = random_walk(40, seed=4).assign(volume=0.0)
    v = IndicatorEngine(SMALL).analyze(b).values
    assert v["relative_volume_4"].isna().all()  # 0 / 0 baseline: undefined
    assert (v["obv"] == 0).all()  # adds sign x 0


# ── numerical edge cases ──


def test_constant_price_series() -> None:
    v = IndicatorEngine(SMALL).analyze(bars([(100, 100, 100, 100)] * 30)).values
    assert (v["sma_5"].dropna() == 100).all() and (v["ema_5"].dropna() == 100).all()
    assert (v["macd"].dropna() == 0).all()
    assert v["rsi_4"].isna().all()  # no gains, no losses: undefined
    assert v["stoch_k"].isna().all()  # zero-range window: undefined
    assert (v["roc_3"].dropna() == 0).all()
    assert (v["atr_4"].dropna() == 0).all() and (v["true_range"].dropna() == 0).all()
    assert (v["bb_width_pct"].dropna() == 0).all() and v["bb_percent_b"].isna().all()
    assert (v["realized_vol_5"].dropna() == 0).all()


@pytest.mark.parametrize("scale", [1e-6, 1e6])
def test_scale_invariance_of_dimensionless_indicators(scale: float) -> None:
    b = random_walk(300, seed=5)
    scaled = b.copy()
    scaled[["open", "high", "low", "close"]] *= scale
    x, y = ENGINE.analyze(b).values, ENGINE.analyze(scaled).values
    for col in ("rsi_14", "stoch_k", "stoch_d", "roc_12", "bb_width_pct", "bb_percent_b",
                "realized_vol_20"):  # fmt: skip
        np.testing.assert_allclose(x[col], y[col], rtol=1e-8, atol=1e-8, err_msg=col)
    np.testing.assert_allclose(x["sma_20"] * scale, y["sma_20"], rtol=1e-12)
    np.testing.assert_allclose(x["atr_14"] * scale, y["atr_14"], rtol=1e-10)


def test_very_small_price_changes_are_defined() -> None:
    candles = [(100 + i * 1e-7, 100 + i * 1e-7, 100 + i * 1e-7, 100 + i * 1e-7) for i in range(40)]
    v = IndicatorEngine(SMALL).analyze(bars(candles)).values
    assert (v["rsi_4"].dropna() == 100).all()  # strictly rising, however slowly
    assert np.isfinite(v["realized_vol_5"].dropna()).all()


def test_no_infinite_values_on_random_data() -> None:
    v = indicator_frame(ENGINE.analyze(random_walk(500, seed=6)))
    assert not np.isinf(v.to_numpy(dtype=float)).any()


def test_source_bars_are_not_modified() -> None:
    b = random_walk(250, seed=7)
    before = b.copy(deep=True)
    ENGINE.analyze(b)
    pd.testing.assert_frame_equal(b, before)


# ── point-in-time / leakage ──


@pytest.mark.parametrize(
    ("factory", "engine"),
    [(lambda: random_walk(260, seed=8), ENGINE), (spy_sample, IndicatorEngine(SMALL))],
)
def test_truncated_history_gives_identical_values_at_every_bar(factory, engine) -> None:  # type: ignore[no-untyped-def]
    full_bars = factory()
    full = indicator_frame(engine.analyze(full_bars))
    for i in range(len(full_bars)):
        partial = indicator_frame(engine.analyze(full_bars.iloc[: i + 1]))
        pd.testing.assert_series_equal(partial.iloc[-1], full.iloc[i], check_names=False)


def test_altering_future_bars_never_changes_the_past() -> None:
    base = random_walk(300, seed=9)
    cut = 210
    reference = indicator_frame(ENGINE.analyze(base))
    rng = np.random.default_rng(10)
    for _ in range(5):
        altered = base.copy()
        future = altered.iloc[cut + 1 :]
        altered.iloc[cut + 1 :, :4] = future.iloc[:, :4].to_numpy() * rng.uniform(
            0.5, 1.5, (len(future), 1)
        )
        altered["high"] = altered[["open", "high", "low", "close"]].max(axis=1)
        altered["low"] = altered[["open", "high", "low", "close"]].min(axis=1)
        altered.iloc[cut + 1 :, 4] = rng.integers(0, 10**7, len(future)).astype(float)
        result = indicator_frame(ENGINE.analyze(altered))
        pd.testing.assert_frame_equal(result.iloc[: cut + 1], reference.iloc[: cut + 1])


def test_centred_window_is_detected_by_the_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: a future-peeking SMA (centred window) must break prefix equivalence."""
    import app.indicators.indicators as ind_module

    def centred_sma(x: pd.Series, n: int) -> pd.Series:
        return x.rolling(n, min_periods=n, center=True).mean()

    monkeypatch.setattr(ind_module, "sma", centred_sma)
    b = random_walk(80, seed=11)
    full = indicator_frame(ENGINE.analyze(b))
    partial = indicator_frame(ENGINE.analyze(b.iloc[:60]))
    with pytest.raises(AssertionError):
        pd.testing.assert_series_equal(partial.iloc[-1], full.iloc[59], check_names=False)


# ── determinism / config / output ──


def test_output_is_deterministic() -> None:
    b = random_walk(400, seed=12)
    x, y = ENGINE.analyze(b), IndicatorEngine().analyze(b.copy())
    pd.testing.assert_frame_equal(x.values, y.values)
    pd.testing.assert_frame_equal(x.catalog, y.catalog)
    assert x.config_fingerprint == y.config_fingerprint and x.engine_version == ENGINE_VERSION


def test_config_changes_columns_and_fingerprint() -> None:
    a = IndicatorEngine(SMALL).analyze(random_walk(60, seed=13))
    assert {"sma_3", "sma_5", "ema_3", "rsi_4", "atr_4", "realized_vol_5"} <= set(a.values.columns)
    assert SMALL.fingerprint() != IndicatorConfig().fingerprint()
    assert IndicatorConfig().fingerprint() == IndicatorConfig().fingerprint()


@pytest.mark.parametrize(
    "overrides",
    [
        {"macd_fast": 26, "macd_slow": 12},
        {"rsi_period": 0},
        {"sma_periods": ()},
        {"ema_periods": (20, 20)},
        {"bollinger_k": 0.0},
        {"bollinger_ddof": 2},
        {"realized_vol_period": 1, "realized_vol_ddof": 1},
    ],
)
def test_invalid_config_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(IndicatorConfig(), **overrides)  # type: ignore[arg-type]


def test_metadata_and_no_trading_fields() -> None:
    a = ENGINE.analyze(random_walk(50, seed=14), instrument_id=3, timeframe="1d")
    assert set(a.values["instrument_id"]) == {3} and set(a.values["timeframe"]) == {"1d"}
    forbidden = ("signal_buy", "buy", "sell", "entry", "exit", "stop", "target", "prediction",
                 "expected", "pnl", "long", "short")  # fmt: skip
    assert not [c for c in a.values.columns if any(f in c for f in forbidden)]
    assert set(a.catalog["family"]) == {"trend", "momentum", "volatility", "volume"}


def test_relative_volume_matches_candlestick_engine_definition() -> None:
    b = random_walk(80, seed=15)
    ours = ENGINE.analyze(b).values["relative_volume_20"]
    candle = CandlestickEngine().analyze(b).geometry["relative_volume"]
    pd.testing.assert_series_equal(ours, candle, check_names=False)


def test_value_as_of() -> None:
    b = random_walk(40, seed=16)
    a = ENGINE.analyze(b)
    pd.testing.assert_series_equal(
        a.value_as_of(b.index[30] + pd.Timedelta(hours=2)), a.values.iloc[30]
    )
    with pytest.raises(LookupError):
        a.value_as_of(b.index[0] - pd.Timedelta(days=1))


# ── regression on real SPY bars (engine 1.0.0) ──


# Engine 1.0.0, default config, SPY 2020-01-27..2020-04-24 (values cross-checked against TA-Lib
# on the full SPY history by scripts/crossvalidate_indicators.py).
SPY_LAST = {
    "sma_20": 269.997002,
    "rsi_14": 55.256189,
    "atr_14": 9.709136,
    "stoch_k": 83.400304,
    "macd": 2.544282,
    "bb_upper": 294.985445,
    "realized_vol_20": 0.435551,
}


def test_spy_regression_snapshot() -> None:
    b = spy_sample()
    v = IndicatorEngine().analyze(b).values.iloc[-1]
    assert {k: round(float(v[k]), 6) for k in SPY_LAST} == SPY_LAST
    # Independent check straight from the fixture: SMA20 = mean of the last 20 closes.
    assert v["sma_20"] == pytest.approx(b["close"].iloc[-20:].sum() / 20, rel=1e-12)
