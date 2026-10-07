"""Features, target, split and preprocessing: timing and leakage guarantees."""

import dataclasses
from datetime import date

import numpy as np
import pandas as pd
import pytest

from app.ml.config import SplitSpec
from app.ml.features import (
    INSUFFICIENT,
    VALID,
    assemble,
    build_features,
    compute_components,
)
from app.ml.preprocessing import fit_preprocessor
from app.ml.split import assert_chronological, assign_periods
from app.ml.target import make_target
from app.price_action.engine import PriceActionEngine
from tests.unit.ml.helpers import SPLIT, walk


@pytest.fixture(scope="module")
def bars() -> pd.DataFrame:
    return walk()


@pytest.fixture(scope="module")
def full(bars: pd.DataFrame):  # type: ignore[no-untyped-def]
    return build_features(bars)


def features_of(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.drop(columns=["observed_at"])


# ── feature matrix ──


def test_catalog_and_layout(full) -> None:  # type: ignore[no-untyped-def]
    names = full.feature_names
    assert len(names) == len(set(names)) == 86
    assert list(full.frame.columns) == ["observed_at", "availability", *names]
    assert {s.group for s in full.specs} == {
        "candle",
        "pattern",
        "indicator",
        "price_action",
        "regime",
    }
    assert not [n for n in names if "outcome" in n or "adj" in n]  # outcomes / adjusted never used


def test_warmup_and_availability(full) -> None:  # type: ignore[no-untyped-def]
    av = full.frame["availability"]
    assert (av.iloc[:272] == INSUFFICIENT).all() and av.iloc[272] == VALID
    required = [s.name for s in full.specs if not s.optional]
    valid = full.frame[av == VALID]
    assert np.isfinite(valid[required].to_numpy(dtype=float)).all()


def test_observed_at_is_session_close(full, bars: pd.DataFrame) -> None:  # type: ignore[no-untyped-def]
    assert (full.frame["observed_at"] > full.frame.index).all()
    assert (full.frame["observed_at"] < full.frame.index + pd.Timedelta(hours=8)).all()


@pytest.mark.parametrize("cut", [400, 650, 900])
def test_prefix_features_unchanged_when_data_appended(full, bars: pd.DataFrame, cut: int) -> None:  # type: ignore[no-untyped-def]
    prefix = build_features(bars.iloc[:cut])
    pd.testing.assert_frame_equal(features_of(prefix.frame), features_of(full.frame.iloc[:cut]))


@pytest.mark.parametrize("columns", [["open", "high", "low", "close"], ["volume"], ["adj_close"]])
def test_future_bar_mutation_does_not_change_past_features(
    full, bars: pd.DataFrame, columns: list[str]
) -> None:  # type: ignore[no-untyped-def]
    cut = 700
    altered = bars.copy()
    rng = np.random.default_rng(1)
    for col in columns:
        altered.iloc[cut + 1 :, altered.columns.get_loc(col)] *= rng.uniform(
            0.5, 1.5, len(bars) - cut - 1
        )
    altered["high"] = altered[["open", "high", "low", "close"]].max(axis=1)
    altered["low"] = altered[["open", "high", "low", "close"]].min(axis=1)
    mutated = build_features(altered)
    pd.testing.assert_frame_equal(
        features_of(mutated.frame.iloc[: cut + 1]), features_of(full.frame.iloc[: cut + 1])
    )
    if columns == ["adj_close"]:  # adjusted close is not a feature input at all
        pd.testing.assert_frame_equal(mutated.frame, full.frame)


@pytest.mark.parametrize(
    "component", ["patterns", "pa_state", "pa_events", "indicators", "regimes", "geometry"]
)
def test_future_component_mutation_does_not_change_past_rows(
    bars: pd.DataFrame, component: str
) -> None:
    comp = compute_components(bars)
    cut = bars.index[700]
    base = assemble(comp, bars["close"])
    frame = getattr(comp, component).copy()
    if component in ("patterns", "pa_events"):
        ts_col = "ts" if component == "patterns" else "available_at"
        frame = frame[frame[ts_col] <= cut]  # drop all future observations/events
        extra = frame.iloc[:0].copy()
        frame = pd.concat([frame, extra])
    else:
        later = frame.index > cut
        for col in frame.columns:
            if pd.api.types.is_float_dtype(frame[col]):
                frame.loc[later, col] = frame.loc[later, col] * 3.0 + 1.0
            elif frame[col].dtype == object:
                frame.loc[later, col] = "insufficient_data"
    mutated = assemble(dataclasses.replace(comp, **{component: frame}), bars["close"])
    pd.testing.assert_frame_equal(
        features_of(mutated.frame.loc[:cut]), features_of(base.frame.loc[:cut])
    )


def test_structure_features_change_only_on_pivot_confirmation(full, bars: pd.DataFrame) -> None:  # type: ignore[no-untyped-def]
    swings = PriceActionEngine().analyze(bars).swings
    cols = [c for c in full.feature_names if c.startswith("structure_")]
    changed = full.frame[cols].ne(full.frame[cols].shift(1)).any(axis=1)
    changed.iloc[0] = False
    positions = {bars.index.get_loc(t) for t in full.frame.index[changed]}
    assert positions <= set(swings["confirmed_idx"])


def test_leaky_feature_is_detected_by_prefix_check(bars: pd.DataFrame) -> None:
    """Control: a feature using close(T+1) breaks prefix equality."""

    def leaky_build(b: pd.DataFrame) -> pd.DataFrame:
        f = build_features(b).frame.copy()
        f["leak_next_return"] = b["close"].shift(-1) / b["close"] - 1
        return f

    full_f, prefix_f = leaky_build(bars), leaky_build(bars.iloc[:700])
    with pytest.raises(AssertionError):
        pd.testing.assert_frame_equal(features_of(prefix_f), features_of(full_f.iloc[:700]))


# ── target ──


def test_target_definition_and_last_row() -> None:
    idx = pd.date_range("2024-01-02 14:30", periods=4, freq="D", tz="UTC")
    close = pd.Series([1.0, 2.0, 2.0, 1.0], index=idx)
    observed = pd.Series(idx + pd.Timedelta(hours=6.5), index=idx)
    t = make_target(close, observed)
    assert t["target"].iloc[:3].tolist() == [1.0, 0.0, 0.0]  # unchanged close counts as 0
    assert np.isnan(t["target"].iloc[-1]) and pd.isna(t["target_timestamp"].iloc[-1])
    assert (t["target_timestamp"].iloc[:3] == observed.iloc[1:].to_numpy()).all()
    assert (t["target_timestamp"].iloc[:3] > observed.iloc[:3]).all()  # strictly after observation


def test_features_do_not_depend_on_the_target(full, bars: pd.DataFrame) -> None:  # type: ignore[no-untyped-def]
    assert not set(full.feature_names) & {"target", "target_timestamp"}


# ── split ──


def test_periods_and_purge(full) -> None:  # type: ignore[no-untyped-def]
    per = assign_periods(pd.DatetimeIndex(full.frame.index), SPLIT)
    idx = full.frame.index
    train_last = idx[per == "train"].max()
    assert train_last.date() <= SPLIT.train_end
    purged = idx[per == "purged"]
    assert len(purged) == 2  # last train row + last validation row
    assert idx[per == "validation"].min() > train_last
    assert idx[per == "test"].min().date() >= SPLIT.test_start


def test_random_or_overlapping_splits_are_rejected(full) -> None:  # type: ignore[no-untyped-def]
    idx = pd.DatetimeIndex(full.frame.index)
    with pytest.raises(ValueError, match="time order"):
        assign_periods(idx[np.random.default_rng(0).permutation(len(idx))], SPLIT)
    rng = np.random.default_rng(1)
    shuffled = rng.permutation(len(idx))
    with pytest.raises(ValueError, match="random/overlapping"):
        assert_chronological(idx[np.sort(shuffled[:500])], idx[np.sort(shuffled[500:])])
    with pytest.raises(ValueError, match="chronological"):
        SplitSpec(train_end=date(2018, 1, 1), validation_start=date(2017, 1, 1))
    with pytest.raises(ValueError):
        SplitSpec(purge_sessions=0)


# ── preprocessing ──


def _train_rows(full) -> pd.Series:  # type: ignore[no-untyped-def]
    per = assign_periods(pd.DatetimeIndex(full.frame.index), SPLIT)
    return (full.frame["availability"] == VALID) & (per == "train")


def test_preprocessor_uses_train_rows_only(full) -> None:  # type: ignore[no-untyped-def]
    rows = _train_rows(full)
    pre = fit_preprocessor(full.X[rows], full.specs)
    assert pre.fitted_rows == int(rows.sum())
    wrecked = full.X.copy()
    wrecked.loc[~rows] = wrecked.loc[~rows] * 100.0 + 7.0  # wreck every non-train row
    again = fit_preprocessor(wrecked[rows], full.specs)
    np.testing.assert_array_equal(pre.means, again.means)
    np.testing.assert_array_equal(pre.scales, again.scales)
    assert pre.fill_values == again.fill_values


def test_full_sample_fit_would_differ(full) -> None:  # type: ignore[no-untyped-def]
    """Control: fitting on all valid rows (leaky) gives different parameters."""
    rows = _train_rows(full)
    valid = full.frame["availability"] == VALID
    a = fit_preprocessor(full.X[rows], full.specs)
    b = fit_preprocessor(full.X[valid], full.specs)
    assert not np.allclose(a.means, b.means)


def test_missing_indicators_and_required_nan(full) -> None:  # type: ignore[no-untyped-def]
    rows = _train_rows(full)
    pre = fit_preprocessor(full.X[rows], full.specs)
    assert "trend_quality__missing" in pre.output_columns
    sample = full.X[rows].iloc[:5].copy()
    sample["trend_quality"] = np.nan
    assert np.isfinite(pre.transform(sample)).all()
    sample["dist_sma_20"] = np.nan  # required feature
    with pytest.raises(ValueError, match="required"):
        pre.transform(sample)
    with pytest.raises(ValueError, match="columns"):
        pre.transform(sample.drop(columns=["gap"]))
