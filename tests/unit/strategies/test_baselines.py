"""Exact rules, boundaries, missing-data handling and feature isolation of each baseline."""

import numpy as np
import pandas as pd
import pytest

from app.strategies.base import FLAT, INSUFFICIENT_DATA, LONG, STATES, Strategy
from app.strategies.baselines import (
    BuyAndHold,
    PriceActionTrend,
    RegimeTrend,
    RsiMomentum,
    SmaCrossover,
    SmaTrend,
    baseline_strategies,
)
from app.strategies.config import StrategyConfig

CFG = StrategyConfig()
NAN = float("nan")


def frame(**cols: list[object]) -> pd.DataFrame:
    return pd.DataFrame(cols)


def test_exactly_six_long_flat_baselines() -> None:
    ids = [s.strategy_id for s in baseline_strategies(CFG)]
    assert ids == ["buy_and_hold", "sma_trend", "sma_crossover", "rsi_momentum",
                   "price_action_trend", "regime_trend"]  # fmt: skip
    assert STATES == ("LONG", "FLAT", "INSUFFICIENT_DATA")  # no SHORT in Phase 7


def test_buy_and_hold() -> None:
    out = BuyAndHold().decide(frame(close=[100.0, NAN, 101.0]))
    assert out.tolist() == [LONG, INSUFFICIENT_DATA, LONG]


def test_sma_trend_rule_and_boundary() -> None:
    f = frame(close=[101.0, 100.0, 99.0, 100.0, NAN], sma_200=[100.0, 100.0, 100.0, NAN, 100.0])
    assert SmaTrend(CFG).decide(f).tolist() == [
        LONG,
        FLAT,
        FLAT,
        INSUFFICIENT_DATA,
        INSUFFICIENT_DATA,
    ]


def test_sma_crossover_rule_and_boundary() -> None:
    f = frame(sma_20=[11.0, 10.0, 9.0, NAN], sma_50=[10.0, 10.0, 10.0, 10.0])
    assert SmaCrossover(CFG).decide(f).tolist() == [LONG, FLAT, FLAT, INSUFFICIENT_DATA]


def test_rsi_momentum_rule_and_boundary() -> None:
    f = frame(rsi_14=[50.0001, 50.0, 49.9, NAN, 100.0])
    assert RsiMomentum(CFG).decide(f).tolist() == [LONG, FLAT, FLAT, INSUFFICIENT_DATA, LONG]


@pytest.mark.parametrize(
    ("label", "expected"),
    [("uptrend", LONG), ("downtrend", FLAT), ("range", FLAT), ("transition", FLAT),
     ("insufficient_data", INSUFFICIENT_DATA), (None, INSUFFICIENT_DATA)],
)  # fmt: skip
def test_price_action_trend(label: str | None, expected: str) -> None:
    assert PriceActionTrend().decide(frame(pa_structure=[label])).iloc[0] == expected


@pytest.mark.parametrize(
    ("label", "expected"),
    [("trending_up", LONG), ("trending_down", FLAT), ("ranging", FLAT), ("transition", FLAT),
     ("high_volatility_transition", FLAT), ("low_volatility_transition", FLAT),
     ("insufficient_data", INSUFFICIENT_DATA)],
)  # fmt: skip
def test_regime_trend(label: str, expected: str) -> None:
    assert RegimeTrend().decide(frame(regime_composite=[label])).iloc[0] == expected


def test_nan_is_never_silently_flat() -> None:
    for s in baseline_strategies(CFG):
        f = pd.DataFrame({c: [NAN] for c in s.features})
        assert s.decide(f).iloc[0] == INSUFFICIENT_DATA, s.strategy_id


# ── isolation: undeclared features cannot influence a rule ──

ALL_FEATURES = ("close", "sma_20", "sma_50", "sma_200", "rsi_14", "pa_structure",
                "regime_composite", "volume", "macd", "atr_14")  # fmt: skip


def random_features(rng: np.random.Generator, n: int = 300) -> pd.DataFrame:
    return pd.DataFrame(
        {
            **{
                c: rng.normal(100, 5, n)
                for c in ("close", "sma_20", "sma_50", "sma_200", "volume", "atr_14")
            },
            "rsi_14": rng.uniform(0, 100, n),
            "macd": rng.normal(0, 1, n),
            "pa_structure": rng.choice(
                ["uptrend", "downtrend", "range", "transition", "insufficient_data"], n
            ),
            "regime_composite": rng.choice(
                ["trending_up", "trending_down", "ranging", "transition"], n
            ),
        }
    )


@pytest.mark.parametrize("strategy", baseline_strategies(CFG), ids=lambda s: s.strategy_id)
def test_only_declared_features_matter(strategy: Strategy) -> None:
    rng = np.random.default_rng(1)
    base = random_features(rng)
    reference = strategy.decide(base)
    for _ in range(5):
        noisy = random_features(rng)
        for col in strategy.features:  # keep declared inputs, scramble everything else
            noisy[col] = base[col]
        pd.testing.assert_series_equal(strategy.decide(noisy), reference)
    assert set(strategy.features) <= set(ALL_FEATURES)


def test_regime_trend_ignores_rsi_even_when_regime_unchanged() -> None:
    f = random_features(np.random.default_rng(2))
    g = f.assign(rsi_14=100 - f["rsi_14"])
    pd.testing.assert_series_equal(RegimeTrend().decide(f), RegimeTrend().decide(g))
