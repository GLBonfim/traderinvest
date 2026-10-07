"""Engine guarantees: calendar timestamps, execution latency, warm-up, Price Action latency,
point-in-time (prefix, future mutation, leaky control), isolation, determinism, contract."""

from datetime import UTC, date, datetime
from typing import ClassVar

import numpy as np
import pandas as pd
import pytest

from app.price_action.engine import PriceActionEngine
from app.strategies.base import FLAT, INSUFFICIENT_DATA, LONG, Strategy
from app.strategies.baselines import SmaTrend
from app.strategies.config import ENGINE_VERSION, StrategyConfig
from app.strategies.engine import SIGNAL_COLUMNS, StrategyEngine, StrategyRun
from tests.unit.price_action.helpers import FAST as PA_FAST
from tests.unit.strategies.helpers import SMALL, XNYS, session_walk, small_engine

ENGINE = small_engine()


def per_strategy(run: StrategyRun) -> dict[str, pd.DataFrame]:
    return {
        sid: g.drop(columns=["strategy_id"]).reset_index(drop=True)
        for sid, g in run.signals.groupby("strategy_id", sort=True)
    }


# ── timestamps from the exchange calendar ──


def test_observed_and_effective_timestamps() -> None:
    bars = session_walk(10, seed=1, start=date(2024, 7, 1))
    s = ENGINE.run(bars).signals
    s = s[s["strategy_id"] == "buy_and_hold"].set_index("bar_ts")
    jul3 = s.loc[pd.Timestamp("2024-07-03 13:30", tz="UTC")]
    assert jul3["observed_at"] == datetime(2024, 7, 3, 17, 0, tzinfo=UTC)  # early close 13:00 ET
    assert jul3["effective_at"] == datetime(2024, 7, 5, 13, 30, tzinfo=UTC)  # skips July 4th
    fri = s.loc[pd.Timestamp("2024-07-05 13:30", tz="UTC")]
    assert fri["effective_at"] == datetime(2024, 7, 8, 13, 30, tzinfo=UTC)  # skips the weekend
    assert (s["observed_at"] > s.index).all()
    assert (s["effective_at"] > s["observed_at"]).all()


def test_last_bar_effective_time_is_known_without_future_bars() -> None:
    bars = session_walk(30, seed=2)
    last = ENGINE.run(bars).signals.iloc[-1]
    expected = XNYS.sessions(bars.index[-1].date(), date(2030, 1, 1))["open_utc"].iloc[1]
    assert last["effective_at"] == expected


def test_input_contract() -> None:
    bars = session_walk(40, seed=3)
    with pytest.raises(ValueError, match="session open"):
        ENGINE.run(bars.set_axis(bars.index + pd.Timedelta(hours=1)))
    with pytest.raises(ValueError, match="daily"):
        ENGINE.run(bars, timeframe="1h")
    with pytest.raises(ValueError, match="closed"):
        ENGINE.run(bars.assign(is_closed=[True] * 39 + [False]))


# ── execution latency ──


def test_state_decided_at_t_applies_only_from_next_session() -> None:
    for g in per_strategy(ENGINE.run(session_walk(200, seed=4))).values():
        assert g["state_in_effect"].iloc[0] == INSUFFICIENT_DATA
        assert g["state_in_effect"].iloc[1:].tolist() == g["state"].iloc[:-1].tolist()


def test_flip_at_t_is_not_in_effect_at_t() -> None:
    g = per_strategy(ENGINE.run(session_walk(200, seed=5)))["sma_trend"]
    flips = g.index[(g["state"] == LONG) & (g["state"].shift(1) == FLAT)]
    assert len(flips) > 0
    for t in flips:
        assert g.loc[t, "state_in_effect"] == FLAT  # information of T not applied at T
        if t + 1 < len(g):
            assert g.loc[t + 1, "state_in_effect"] == LONG
            assert g.loc[t + 1, "bar_ts"] == g.loc[t, "effective_at"]


# ── warm-up ──


def test_warmup_is_insufficient_data_not_flat() -> None:
    run = ENGINE.run(session_walk(200, seed=6))
    first = dict(zip(run.summary["strategy_id"], run.summary["first_decision_bar"], strict=True))
    assert first["buy_and_hold"] == 0
    assert first["sma_trend"] == SMALL.sma_trend_period - 1
    assert first["sma_crossover"] == SMALL.crossover_slow_period - 1
    assert first["rsi_momentum"] == SMALL.rsi_period
    for sid, g in per_strategy(run).items():
        assert (g["state"].iloc[: first[sid]] == INSUFFICIENT_DATA).all(), sid


# ── Price Action confirmation latency ──


def test_price_action_long_never_precedes_pivot_confirmation() -> None:
    bars = session_walk(300, seed=7)
    g = per_strategy(ENGINE.run(bars))["price_action_trend"]
    swings = PriceActionEngine(PA_FAST).analyze(bars).swings
    starts = g.index[(g["state"] == LONG) & (g["state"].shift(1) != LONG)]
    assert len(starts) > 0
    confirmation_bars = set(swings["confirmed_idx"])
    for t in starts:
        assert t in confirmation_bars  # LONG begins only when a pivot is confirmed
    # The pivot bar itself (5 bars earlier) never already shows the state that its
    # confirmation creates: compare with a run truncated at the pivot bar.
    t0 = int(starts[0])
    pivot_bar = t0 - PA_FAST.swing_right_bars
    early = per_strategy(ENGINE.run(bars.iloc[: pivot_bar + 1]))["price_action_trend"]
    assert early["state"].iloc[-1] == g.loc[pivot_bar, "state"]
    assert g.loc[pivot_bar, "state"] != LONG


# ── point-in-time ──


def test_prefix_equals_full_history_for_every_strategy() -> None:
    bars = session_walk(160, seed=8)
    full = per_strategy(ENGINE.run(bars))
    for i in range(0, len(bars)):
        partial = per_strategy(ENGINE.run(bars.iloc[: i + 1]))
        for sid, g in full.items():
            pd.testing.assert_series_equal(partial[sid].iloc[-1], g.iloc[i], check_names=False)


def test_future_mutation_never_changes_past_states() -> None:
    base = session_walk(220, seed=9)
    cut = 150
    reference = per_strategy(ENGINE.run(base))
    rng = np.random.default_rng(10)
    for _ in range(4):
        altered = base.copy()
        fut = altered.iloc[cut + 1 :]
        altered.iloc[cut + 1 :, :4] = fut.iloc[:, :4].to_numpy() * rng.uniform(
            0.5, 1.5, (len(fut), 1)
        )
        altered["high"] = altered[["open", "high", "low", "close"]].max(axis=1)
        altered["low"] = altered[["open", "high", "low", "close"]].min(axis=1)
        altered.iloc[cut + 1 :, 4] = rng.integers(0, 10**7, len(fut)).astype(float)
        result = per_strategy(ENGINE.run(altered))
        for sid in reference:
            pd.testing.assert_frame_equal(
                result[sid].iloc[: cut + 1], reference[sid].iloc[: cut + 1]
            )


class _PeekingTrend(Strategy):
    """Deliberately leaky: compares TOMORROW's close with today's SMA."""

    strategy_id: ClassVar[str] = "leaky"
    version: ClassVar[str] = "0"
    description: ClassVar[str] = "control"

    @property
    def features(self) -> tuple[str, ...]:
        return ("close", "sma_20")

    def decide(self, features: pd.DataFrame) -> pd.Series:
        nxt = features["close"].shift(-1)
        out = np.where(
            features["sma_20"].isna(),
            INSUFFICIENT_DATA,
            np.where(nxt > features["sma_20"], LONG, FLAT),
        )
        return pd.Series(out, index=features.index, dtype=object)


def test_harness_detects_a_future_peeking_strategy() -> None:
    eng = small_engine(strategies=(_PeekingTrend(),))
    bars = session_walk(120, seed=11)
    full = per_strategy(eng.run(bars))["leaky"]
    mismatches = sum(
        not per_strategy(eng.run(bars.iloc[: i + 1]))["leaky"].iloc[-1].equals(full.iloc[i])
        for i in range(20, 120)
    )
    assert mismatches > 0


# ── isolation at engine level ──


class _Rogue(Strategy):
    strategy_id: ClassVar[str] = "rogue"
    version: ClassVar[str] = "0"
    description: ClassVar[str] = "reads an undeclared column"

    @property
    def features(self) -> tuple[str, ...]:
        return ("close",)

    def decide(self, features: pd.DataFrame) -> pd.Series:
        return features["sma_20"]  # not declared


def test_engine_passes_only_declared_columns() -> None:
    with pytest.raises(KeyError):
        small_engine(strategies=(_Rogue(), SmaTrend(SMALL))).run(session_walk(40, seed=12))


def test_volume_never_affects_any_baseline() -> None:
    bars = session_walk(200, seed=13)
    a = per_strategy(ENGINE.run(bars))
    b = per_strategy(ENGINE.run(bars.assign(volume=np.nan)))
    for sid in a:
        pd.testing.assert_frame_equal(a[sid], b[sid])


def test_only_required_engines_are_run() -> None:
    run = small_engine(strategies=(SmaTrend(SMALL),)).run(session_walk(40, seed=14))
    assert list(run.features.columns) == ["close", "sma_20"]


# ── determinism / config / contract ──


def test_deterministic_and_fingerprinted() -> None:
    bars = session_walk(150, seed=15)
    x, y = ENGINE.run(bars), small_engine().run(bars.copy())
    pd.testing.assert_frame_equal(x.signals, y.signals)
    assert x.config_fingerprint == y.config_fingerprint and x.engine_version == ENGINE_VERSION
    other = StrategyEngine(StrategyConfig(rsi_period=10)).fingerprint()
    assert StrategyEngine().fingerprint() != other
    assert StrategyEngine().fingerprint() == StrategyEngine().fingerprint()
    assert small_engine(strategies=(SmaTrend(SMALL),)).fingerprint() != ENGINE.fingerprint()


@pytest.mark.parametrize(
    "kw",
    [
        {"crossover_fast_period": 50, "crossover_slow_period": 20},
        {"sma_trend_period": 0},
        {"rsi_midline": 100.0},
    ],
)
def test_invalid_config(kw: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        StrategyConfig(**kw)  # type: ignore[arg-type]


def test_output_contract() -> None:
    run = ENGINE.run(session_walk(60, seed=16), instrument_id=9)
    assert tuple(run.signals.columns) == SIGNAL_COLUMNS
    forbidden = ("price", "stop", "target", "size", "quantity", "order", "fill", "pnl",
                 "return", "probability", "confidence", "score", "short")  # fmt: skip
    assert not [c for c in SIGNAL_COLUMNS if any(f in c for f in forbidden)]
    assert set(run.signals["state"]) <= {LONG, FLAT, INSUFFICIENT_DATA}
    assert set(run.signals["instrument_id"]) == {9}
    row = run.signals[run.signals["strategy_id"] == "sma_trend"].iloc[-1]
    assert row["reason"].startswith("close=") and ";sma_20=" in row["reason"]
    counts = run.summary.set_index("strategy_id")
    assert (counts[["n_long", "n_flat", "n_insufficient_data"]].sum(axis=1) == 60).all()
