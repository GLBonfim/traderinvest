"""Engine-level guarantees: integration with upstream engines, warm-up, edge cases,
point-in-time (prefix, future mutation, future-peeking control), determinism, output contract."""

import dataclasses

import numpy as np
import pandas as pd
import pytest

from app.indicators.engine import IndicatorEngine
from app.price_action.engine import PriceActionEngine
from app.regimes import regimes as rg
from app.regimes.config import ENGINE_VERSION, RegimeConfig
from app.regimes.engine import DIMENSIONS, RegimeAnalysis, RegimeEngine
from tests.unit.candles.helpers import bars
from tests.unit.candles.test_engine import random_walk
from tests.unit.indicators.test_engine import SMALL as IND_SMALL
from tests.unit.price_action.helpers import FAST as PA_FAST

CFG = RegimeConfig(volatility_lookback=40, volatility_min_observations=20)


def engine(cfg: RegimeConfig = CFG) -> RegimeEngine:
    return RegimeEngine(cfg, price_action_config=PA_FAST, indicator_config=IND_SMALL)


def label_frame(a: RegimeAnalysis) -> pd.DataFrame:
    return a.state.drop(columns=["instrument_id", "timeframe"])


# ── integration with upstream engines (no duplicated formulas) ──


def test_dimensions_are_taken_from_upstream_engines() -> None:
    b = random_walk(300, seed=1)
    st = engine().analyze(b).state
    pa = PriceActionEngine(PA_FAST).analyze(b).state
    ind = IndicatorEngine(IND_SMALL).analyze(b).values
    pd.testing.assert_series_equal(
        st["trend_regime"], pa["structure"].astype(object), check_names=False
    )
    pd.testing.assert_series_equal(
        st["volatility_measure"], ind["realized_vol_5"], check_names=False
    )
    pd.testing.assert_series_equal(st["momentum_rsi"], ind["rsi_4"], check_names=False)
    pd.testing.assert_series_equal(st["momentum_macd"], ind["macd"], check_names=False)
    pd.testing.assert_series_equal(
        st["relative_volume"], ind["relative_volume_4"], check_names=False
    )


def test_price_action_outputs_do_not_depend_on_volume() -> None:
    """Justifies passing missing volume to the Price Action Engine as 0 (never reaches outputs)."""
    b = random_walk(250, seed=2)
    pa = PriceActionEngine(PA_FAST)
    x = pa.analyze(b)
    y = pa.analyze(b.assign(volume=0.0))
    for attr in ("swings", "state", "zones", "events", "outcomes"):
        pd.testing.assert_frame_equal(getattr(x, attr), getattr(y, attr))


# ── warm-up ──


def test_warmup_boundaries() -> None:
    st = engine().analyze(random_walk(200, seed=3)).state
    vol_first = IND_SMALL.realized_vol_period + CFG.volatility_min_observations  # 5 + 20
    vol = st["volatility_regime"]
    assert (vol.iloc[:vol_first] == "insufficient_data").all()
    assert (vol.iloc[vol_first:] != "insufficient_data").all()
    mom_first = max(IND_SMALL.macd_slow - 1, IND_SMALL.rsi_period, IND_SMALL.roc_period)
    mom = st["momentum_regime"]
    assert (mom.iloc[:mom_first] == "insufficient_data").all()
    assert (mom.iloc[mom_first:] != "insufficient_data").all()
    part = st["participation_regime"]
    assert (part.iloc[: IND_SMALL.relative_volume_period] == "insufficient_data").all()
    comp_ok = (st["trend_regime"] != "insufficient_data") & (vol != "insufficient_data")
    assert ((st["composite_regime"] == "insufficient_data") == ~comp_ok).all()


def test_default_volatility_warmup() -> None:
    st = RegimeEngine().analyze(random_walk(300, seed=4)).state
    first = st.index.get_loc(st.index[st["volatility_regime"] != "insufficient_data"][0])
    assert first == 20 + 252


# ── edge cases ──


def test_flat_prices() -> None:
    st = engine().analyze(bars([(100, 100, 100, 100)] * 80)).state
    assert set(st["trend_regime"]) == {"insufficient_data"}  # no pivots
    assert set(st["momentum_regime"]) == {"insufficient_data"}  # RSI undefined
    assert set(st["volatility_regime"].iloc[25:]) == {"normal"}  # zero vol vs zero vol: mid-rank
    assert set(st["composite_regime"]) == {"insufficient_data"}


def test_constant_volatility_is_normal() -> None:
    candles = [(100 * 1.01**i, 100 * 1.01**i, 100 * 1.01**i, 100 * 1.01**i) for i in range(80)]
    st = engine().analyze(bars(candles)).state
    assert set(st["volatility_regime"].iloc[25:]) == {"normal"}


def test_abrupt_volatility_change() -> None:
    rng = np.random.default_rng(5)
    calm = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, 60)))
    wild = calm[-1] * np.exp(np.cumsum(rng.normal(0, 0.05, 10)))
    closes = np.r_[calm, wild]
    candles = [(c, c * 1.001, c * 0.999, c) for c in closes]
    st = engine().analyze(bars(candles)).state
    assert st["volatility_regime"].iloc[64:].isin(["high", "extreme"]).all()
    jump = st["volatility_changed"].iloc[60:66]
    assert jump.any()


def test_missing_volume_only_affects_participation() -> None:
    b = random_walk(200, seed=6)
    full = label_frame(engine().analyze(b))
    gap = b.copy()
    gap.iloc[50:60, gap.columns.get_loc("volume")] = np.nan
    missing = label_frame(engine().analyze(gap))
    vol_cols = [
        "relative_volume",
        "participation_regime",
        "participation_changed",
        "participation_age",
    ]
    pd.testing.assert_frame_equal(missing.drop(columns=vol_cols), full.drop(columns=vol_cols))
    assert (missing["participation_regime"].iloc[50:64] == "insufficient_data").all()
    assert not gap["volume"].iloc[:50].isna().any()  # input not modified by the engine


def test_zero_volume_participation_is_insufficient() -> None:
    st = engine().analyze(random_walk(60, seed=7).assign(volume=0.0)).state
    assert set(st["participation_regime"]) == {"insufficient_data"}  # 0 / 0 baseline


@pytest.mark.parametrize(("col", "value"), [("close", np.nan), ("high", np.inf)])
def test_invalid_prices_rejected(col: str, value: float) -> None:
    b = random_walk(60, seed=8)
    b.iloc[30, b.columns.get_loc(col)] = value
    with pytest.raises(ValueError):
        engine().analyze(b)


# ── point-in-time ──


def test_prefix_equals_full_history_at_every_bar() -> None:
    b = random_walk(220, seed=9)
    eng = engine()
    full = label_frame(eng.analyze(b))
    for i in range(len(b)):
        partial = label_frame(eng.analyze(b.iloc[: i + 1]))
        pd.testing.assert_series_equal(partial.iloc[-1], full.iloc[i], check_names=False)


def test_future_mutation_never_changes_past_regimes() -> None:
    base = random_walk(260, seed=10)
    cut = 180
    eng = engine()
    reference = label_frame(eng.analyze(base))
    rng = np.random.default_rng(11)
    for _ in range(5):
        altered = base.copy()
        future = altered.iloc[cut + 1 :]
        altered.iloc[cut + 1 :, :4] = future.iloc[:, :4].to_numpy() * rng.uniform(
            0.5, 1.5, (len(future), 1)
        )
        altered["high"] = altered[["open", "high", "low", "close"]].max(axis=1)
        altered["low"] = altered[["open", "high", "low", "close"]].min(axis=1)
        altered.iloc[cut + 1 :, 4] = rng.integers(0, 10**7, len(future)).astype(float)
        result = label_frame(eng.analyze(altered))
        pd.testing.assert_frame_equal(result.iloc[: cut + 1], reference.iloc[: cut + 1])


def test_full_sample_percentile_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: ranking against the full sample (incl. the future) must break prefix equality."""
    import app.regimes.engine as engine_module

    def full_sample_rank(x: pd.Series, lookback: int, min_observations: int) -> pd.DataFrame:
        pct = x.rank(pct=True)  # uses every value, including later ones
        return pd.DataFrame({"percentile": pct, "reference_count": x.notna().sum()}, index=x.index)

    monkeypatch.setattr(engine_module.rg, "causal_percentile_rank", full_sample_rank)
    b = random_walk(120, seed=12)
    eng = engine()
    full = label_frame(eng.analyze(b))
    mismatches = 0
    for i in range(30, len(b), 5):
        partial = label_frame(eng.analyze(b.iloc[: i + 1]))
        if not partial.iloc[-1].equals(full.iloc[i]):
            mismatches += 1
    assert mismatches > 0


def test_trend_changes_only_when_a_pivot_is_confirmed() -> None:
    b = random_walk(300, seed=13)
    st = engine().analyze(b).state
    swings = PriceActionEngine(PA_FAST).analyze(b).swings
    confirmation_bars = set(swings["confirmed_idx"])
    changed_at = {b.index.get_loc(ts) for ts in st.index[st["trend_changed"]]}
    assert changed_at <= confirmation_bars


# ── determinism / config / output ──


def test_deterministic() -> None:
    b = random_walk(200, seed=14)
    x, y = engine().analyze(b), engine().analyze(b.copy())
    pd.testing.assert_frame_equal(x.state, y.state)
    assert x.config_fingerprint == y.config_fingerprint and x.engine_version == ENGINE_VERSION


def test_fingerprint_covers_upstream_configs() -> None:
    base = RegimeEngine().fingerprint()
    assert RegimeEngine().fingerprint() == base
    assert (
        RegimeEngine(dataclasses.replace(RegimeConfig(), volatility_lookback=500)).fingerprint()
        != base
    )
    assert RegimeEngine(price_action_config=PA_FAST).fingerprint() != base
    assert RegimeEngine(indicator_config=IND_SMALL).fingerprint() != base


@pytest.mark.parametrize(
    "overrides",
    [
        {"volatility_lookback": 0},
        {"volatility_min_observations": 1000},
        {"volatility_low_below": 0.9},
        {"momentum_rsi_extreme_high": 40.0},
        {"participation_high_from": 0.9},
    ],
)
def test_invalid_config(overrides: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(RegimeConfig(), **overrides)


def test_output_contract() -> None:
    a = engine().analyze(random_walk(80, seed=15), instrument_id=5)
    st = a.state
    for dim in DIMENSIONS:
        assert {f"{dim}_regime", f"{dim}_changed", f"{dim}_age"} <= set(st.columns)
    assert set(st["instrument_id"]) == {5}
    forbidden = ("signal", "buy", "sell", "entry", "exit", "stop", "target", "prediction",
                 "expected", "profit", "long", "short", "position")  # fmt: skip
    assert not [c for c in st.columns if any(f in c for f in forbidden)]
    labels = set().union(*(set(st[f"{d}_regime"]) for d in DIMENSIONS))
    assert not [lbl for lbl in labels if any(f in lbl for f in forbidden)]
    pd.testing.assert_series_equal(a.state_as_of(st.index[40] + pd.Timedelta(hours=1)), st.iloc[40])


def test_transition_flags_match_label_changes() -> None:
    st = engine().analyze(random_walk(250, seed=16)).state
    for dim in DIMENSIONS:
        labels = st[f"{dim}_regime"]
        expected = labels.ne(labels.shift(1))
        expected.iloc[0] = False
        pd.testing.assert_series_equal(st[f"{dim}_changed"], expected, check_names=False)
        pd.testing.assert_frame_equal(
            rg.transitions(labels),
            st[[f"{dim}_changed", f"{dim}_age"]].set_axis(["changed", "age"], axis=1),
        )
