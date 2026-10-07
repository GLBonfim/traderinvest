"""Engine-level guarantees: point-in-time (with a leaky control), determinism, configuration,
input checks, output contract and a regression snapshot on real SPY bars."""

import dataclasses

import numpy as np
import pandas as pd
import pytest

from app.price_action.config import ENGINE_VERSION, PriceActionConfig
from app.price_action.engine import PriceActionAnalysis, PriceActionEngine
from tests.unit.candles.test_engine import random_walk, spy_sample
from tests.unit.price_action.helpers import FAST

ENGINE = PriceActionEngine()


def known_at(a: PriceActionAnalysis, ts: pd.Timestamp) -> dict[str, pd.DataFrame]:
    """Everything an analysis says was AVAILABLE at or before ts."""
    outcomes = a.outcomes[a.outcomes["outcome_at"].notna() & (a.outcomes["outcome_at"] <= ts)]
    return {
        "swings": a.swings[a.swings["available_at"] <= ts].reset_index(drop=True),
        "events": a.events[a.events["available_at"] <= ts].reset_index(drop=True),
        "outcomes": outcomes.reset_index(drop=True),
    }


def assert_same_knowledge(x: PriceActionAnalysis, y: PriceActionAnalysis, ts: pd.Timestamp) -> None:
    kx, ky = known_at(x, ts), known_at(y, ts)
    for key in kx:
        pd.testing.assert_frame_equal(kx[key], ky[key], check_dtype=False, obj=key)
    pd.testing.assert_series_equal(x.state.loc[ts], y.state.loc[ts], check_dtype=False)


# ── point-in-time / leakage ──


@pytest.mark.parametrize(
    ("factory", "engine"),
    [(lambda: random_walk(220, seed=21), PriceActionEngine(FAST)), (spy_sample, ENGINE)],
)
def test_truncated_history_gives_identical_knowledge_at_every_bar(factory, engine) -> None:  # type: ignore[no-untyped-def]
    full_bars = factory()
    full = engine.analyze(full_bars)
    for i in range(len(full_bars)):
        ts = full_bars.index[i]
        partial = engine.analyze(full_bars.iloc[: i + 1])
        assert_same_knowledge(partial, full, ts)
        # Outcomes not yet known at ts must be pending in the truncated analysis.
        later = full.outcomes[
            full.outcomes["outcome_at"].isna() | (full.outcomes["outcome_at"] > ts)
        ]
        open_ids = set(later["event_id"]) & set(partial.events["event_id"])
        pending = partial.outcomes[partial.outcomes["event_id"].isin(open_ids)]
        assert set(pending["outcome"]) <= {"pending"}


def test_altering_future_bars_never_changes_the_past() -> None:
    base = random_walk(260, seed=31)
    cut = 150
    engine = PriceActionEngine(FAST)
    reference = engine.analyze(base)
    rng = np.random.default_rng(5)
    for _ in range(5):
        altered = base.copy()
        future = altered.iloc[cut + 1 :]
        scale = rng.uniform(0.6, 1.4, size=(len(future), 1))
        altered.iloc[cut + 1 :, :4] = future.iloc[:, :4].to_numpy() * scale
        altered["high"] = altered[["open", "high", "low", "close"]].max(axis=1)
        altered["low"] = altered[["open", "high", "low", "close"]].min(axis=1)
        result = engine.analyze(altered)
        for ts in base.index[: cut + 1 : 10]:
            assert_same_knowledge(result, reference, ts)
        pd.testing.assert_frame_equal(
            result.state.iloc[: cut + 1], reference.state.iloc[: cut + 1], check_dtype=False
        )


def test_leaky_pivot_confirmation_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: if pivots were treated as known at the pivot bar, the harness must fail."""
    import app.price_action.engine as engine_module
    from app.price_action.swings import detect_swings

    def leaky(bars: pd.DataFrame, cfg: PriceActionConfig) -> pd.DataFrame:
        s = detect_swings(bars, cfg)
        s["confirmed_idx"] = s["pivot_idx"]  # pretends the pivot is known immediately
        s["confirmed_at"] = s["pivot_ts"]
        return s

    monkeypatch.setattr(engine_module, "detect_swings", leaky)
    b = random_walk(120, seed=4)
    engine = PriceActionEngine(FAST)
    full = engine.analyze(b)
    mismatches = 0
    for i in range(20, len(b)):
        try:
            assert_same_knowledge(engine.analyze(b.iloc[: i + 1]), full, b.index[i])
        except AssertionError:
            mismatches += 1
    assert mismatches > 0


def test_pivots_are_never_available_before_confirmation() -> None:
    a = PriceActionEngine(FAST).analyze(random_walk(300, seed=9))
    s = a.swings
    assert ((s["confirmed_idx"] - s["pivot_idx"]) == FAST.swing_right_bars).all()
    assert (s["available_at"] > s["pivot_ts"]).all()


def test_outcomes_are_always_after_their_event() -> None:
    a = PriceActionEngine(FAST).analyze(random_walk(400, seed=13))
    resolved = a.outcomes[a.outcomes["outcome"] != "pending"]
    assert not resolved.empty
    assert (resolved["outcome_at"] > resolved["event_ts"]).all()
    assert (resolved["bars_to_outcome"] >= 1).all()


# ── determinism / config / input ──


def test_output_is_deterministic() -> None:
    b = random_walk(300, seed=17)
    x, y = ENGINE.analyze(b), PriceActionEngine().analyze(b.copy())
    for attr in ("swings", "state", "zones", "events", "outcomes"):
        pd.testing.assert_frame_equal(getattr(x, attr), getattr(y, attr))
    assert x.config_fingerprint == y.config_fingerprint
    assert x.engine_version == ENGINE_VERSION


def test_fingerprint_tracks_thresholds() -> None:
    assert PriceActionConfig().fingerprint() == PriceActionConfig().fingerprint()
    assert FAST.fingerprint() != PriceActionConfig().fingerprint()


@pytest.mark.parametrize(
    "overrides",
    [
        {"swing_left_bars": 0},
        {"zone_tolerance_pct": 0.0},
        {"range_max_width_pct": 1.5},
        {"expansion_min_ratio": 0.9},
        {"contraction_max_ratio": 1.2},
    ],
)
def test_invalid_config_rejected(overrides: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        dataclasses.replace(PriceActionConfig(), **overrides)


def test_input_validation() -> None:
    b = random_walk(30, seed=1)
    with pytest.raises(ValueError, match="UTC"):
        ENGINE.analyze(b.tz_convert("America/New_York"))
    with pytest.raises(ValueError, match="strictly increasing"):
        ENGINE.analyze(b.iloc[::-1])
    with pytest.raises(ValueError, match="closed"):
        ENGINE.analyze(b.assign(is_closed=[True] * 29 + [False]))
    bad = b.copy()
    bad.iloc[5, 3] = np.nan
    with pytest.raises(ValueError, match="missing"):
        ENGINE.analyze(bad)


def test_state_as_of() -> None:
    b = random_walk(50, seed=2)
    a = ENGINE.analyze(b)
    mid = b.index[20] + pd.Timedelta(hours=3)  # between bars 20 and 21
    pd.testing.assert_series_equal(a.state_as_of(mid), a.state.iloc[20])
    with pytest.raises(LookupError):
        a.state_as_of(b.index[0] - pd.Timedelta(days=1))


def test_state_contains_no_trading_fields() -> None:
    a = ENGINE.analyze(random_walk(60, seed=3))
    forbidden = ("signal", "buy", "sell", "entry", "stop", "target", "position_size", "pnl")
    assert not [c for c in a.state.columns if any(f in c for f in forbidden)]


# ── regression on real SPY bars (engine 1.0.0, default config) ──

# Known context: 2020-02-19 is the pre-crash top, 2020-03-23 the March 2020 low.
EXPECTED_SPY_SWINGS = (
    ("2020-02-19", "high", None),
    ("2020-02-28", "low", None),
    ("2020-03-23", "low", "LL"),
    ("2020-04-17", "high", "LH"),
)


def test_spy_regression_snapshot() -> None:
    a = ENGINE.analyze(spy_sample())
    s = a.swings
    got = tuple(
        (ts.strftime("%Y-%m-%d"), kind, label if isinstance(label, str) else None)
        for ts, kind, label in zip(s["pivot_ts"], s["kind"], s["label"], strict=True)
    )
    assert got == EXPECTED_SPY_SWINGS
