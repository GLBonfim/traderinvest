import ast
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.fx.research.m5_lesson_training import (
    NAMES,
    fitted_model,
    labels,
    lesson_features,
    purged_mask,
    score,
)
from app.fx.research.m5_validation import BAR, PROTOCOL, fee_floor, simulate
from tests.unit.fx.test_m5_runtime import native


def pattern_zeros(index):
    return pd.DataFrame(
        {k: 0.0 for k in ("reversal_bear", "continuation_bear", "momentum_bear")}, index=index
    )


def test_lesson_features_do_not_see_future_or_incomplete_h4():
    raw = native(n=400)
    mid = pd.DataFrame(
        {k: (raw["bid_" + k] + raw["ask_" + k]) / 2 for k in ("open", "high", "low", "close")},
        index=raw.index,
    )
    p = pattern_zeros(mid.index)
    before = lesson_features(mid.iloc[:250], p.iloc[:250])
    mid.iloc[250:] *= 3
    pd.testing.assert_frame_equal(before, lesson_features(mid, p).iloc[:250])
    assert before.h4_available.sum() > 0
    missing = mid.drop(mid.index[50])
    out = lesson_features(missing, pattern_zeros(missing.index))
    block = mid.index[50].floor("4h") + pd.Timedelta(hours=4)
    assert (
        out.loc[(out.index >= block) & (out.index < block + pd.Timedelta(hours=4)), "h4_available"]
        == 0
    ).all()


def test_labels_match_independent_existing_simulator_and_stop_wins_tie():
    raw = native(n=300)
    f = pd.DataFrame({"atr": 0.0004}, index=raw.index)
    for j in (10, 40, 80):
        sd = fee_floor("EURUSD")
        entry = float(raw.bid_open.iloc[j + 1])
        raw.loc[raw.index[j + 1], "ask_high"] = entry + 2 * sd
        raw.loc[raw.index[j + 1], "ask_low"] = entry - 3 * sd
    targets = labels(raw, f, "EURUSD")
    for j in (10, 40, 80):
        ss = pd.DataFrame(
            {"dir": 0, "stop_dist": sd, "target_dist": 2 * sd, "score": 0.9, "max_hold": 96},
            index=raw.index,
        )
        ss.loc[raw.index[j], "dir"] = -1
        trades = simulate(
            {"EURUSD": raw}, {"EURUSD": ss}, raw.index[0], raw.index[-1] + BAR, PROTOCOL.base_costs
        )
        assert len(trades) == 1
        assert trades.iloc[0]["reason"] == "stop"
        assert targets.net_r.iloc[j] == pytest.approx(trades.iloc[0]["r_net"], abs=1e-12)
        assert targets.net_r.iloc[j] < -1


def test_labels_exclude_gaps_and_unfinished_horizons():
    raw = native(n=300)
    raw = raw.drop(raw.index[50])
    target = labels(raw, pd.DataFrame({"atr": 0.0004}, index=raw.index), "EURUSD")
    assert np.isnan(target.net_r.iloc[10])
    assert target.net_r.iloc[-96:].isna().all()


def test_full_horizon_purge_and_embargo_are_strict():
    idx = pd.date_range("2023-12-30", periods=900, freq="5min", tz="UTC")
    target = pd.DataFrame({"net_r": 1.0, "label_end": idx + 97 * BAR}, index=idx)
    cutoff = pd.Timestamp("2024-01-01", tz="UTC")
    m = purged_mask(idx, target, cutoff)
    assert m.any()
    assert (target.loc[m, "label_end"] < cutoff - pd.Timedelta(hours=24)).all()


@pytest.mark.parametrize("role,variant", [("reversao", 1), ("continuacao", 200), ("impulso", 1)])
def test_training_changes_weights_and_keeps_artifact_offline(role, variant):
    rng = np.random.default_rng(7)
    x = rng.normal(size=(700, len(NAMES)))
    y = 2 * x[:, 0] + rng.normal(size=700) * 0.1
    fit, artifact = fitted_model(role, variant, x, y)
    pred = score(fit, x, role)
    assert np.std(pred) > 0
    assert artifact["execution_enabled"] is False and artifact["offline_only"] is True
    assert artifact["training_rows"] == 700


def test_research_module_cannot_send_orders_or_import_broker():
    source = Path("app/fx/research/m5_lesson_training.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    imported += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not any("ibkr" in n or "ib_async" in n for n in imported)
    assert "placeOrder(" not in source


def test_weighted_specialty_examples_change_fitted_probability():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(700, len(NAMES)))
    y = np.where(np.arange(700) % 2, 1.0, -1.0)
    fit1, _ = fitted_model("impulso", 1, x, y)
    weights = np.where(y > 0, 5.0, 1.0)
    fit2, _ = fitted_model("impulso", 1, x, y, sample_weight=weights)
    assert score(fit2, x, "impulso").mean() > score(fit1, x, "impulso").mean()
