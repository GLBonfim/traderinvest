"""Training isolation, determinism, prediction states, Phase 8 execution and the full report."""

import dataclasses
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.backtest.config import BacktestConfig
from app.backtest.engine import Backtester
from app.ml.config import EdgeRule, MLConfig
from app.ml.engine import MLExperiment, MLReport
from app.ml.training import predict_proba, states_from_probabilities, train_model, training_mask
from tests.unit.ml.helpers import CFG, VCFG, walk
from tests.unit.strategies.helpers import small_engine


@pytest.fixture(scope="module")
def prepared():  # type: ignore[no-untyped-def]
    exp = MLExperiment(CFG, validation_config=VCFG, strategy_engine=small_engine())
    return exp, walk(), *exp.prepare(walk())


@pytest.fixture(scope="module")
def report(prepared) -> MLReport:  # type: ignore[no-untyped-def]
    exp, bars, *_ = prepared
    return exp.run(bars)


# ── training isolation ──


@pytest.mark.parametrize("model", ["logistic_regression", "random_forest"])
def test_training_is_deterministic(prepared, model: str) -> None:  # type: ignore[no-untyped-def]
    _, _, ds, tgt, per = prepared
    a = predict_proba(train_model(model, CFG, ds, tgt["target"], per), ds)
    b = predict_proba(train_model(model, CFG, ds, tgt["target"], per), ds)
    pd.testing.assert_series_equal(a, b)  # bit-identical (tolerance 0)


@pytest.mark.parametrize("model", ["logistic_regression", "random_forest"])
def test_validation_and_test_rows_cannot_influence_training(prepared, model: str) -> None:  # type: ignore[no-untyped-def]
    _, _, ds, tgt, per = prepared
    base = train_model(model, CFG, ds, tgt["target"], per)
    later = per.isin(["validation", "test"])
    wrecked_frame = ds.frame.copy()
    numeric = [s.name for s in ds.specs if s.kind == "numeric"]
    wrecked_frame.loc[later, numeric] = wrecked_frame.loc[later, numeric] * 50 + 3
    wrecked = dataclasses.replace(ds, frame=wrecked_frame)
    flipped = tgt["target"].where(~later, 1 - tgt["target"])  # future target mutation
    other = train_model(model, CFG, wrecked, flipped, per)
    train_rows = training_mask(ds, tgt["target"], per)
    pd.testing.assert_series_equal(
        predict_proba(base, ds)[train_rows], predict_proba(other, ds)[train_rows]
    )
    np.testing.assert_array_equal(base.preprocessor.means, other.preprocessor.means)


def test_training_rows_are_train_period_only(prepared) -> None:  # type: ignore[no-untyped-def]
    _, _, ds, tgt, per = prepared
    tm = train_model("logistic_regression", CFG, ds, tgt["target"], per)
    assert set(per[tm.train_index]) == {"train"}
    assert tm.train_index.max().date() <= CFG.split.train_end


def test_states_rule() -> None:
    p = pd.Series([0.7, 0.5, 0.49, np.nan])
    assert states_from_probabilities(p, 0.5).tolist() == [
        "LONG",
        "FLAT",
        "FLAT",
        "INSUFFICIENT_DATA",
    ]


def test_last_row_is_predicted_but_never_scored(prepared) -> None:  # type: ignore[no-untyped-def]
    _, _, ds, tgt, per = prepared
    tm = train_model("logistic_regression", CFG, ds, tgt["target"], per)
    p = predict_proba(tm, ds)
    assert np.isnan(tgt["target"].iloc[-1]) and np.isfinite(p.iloc[-1])


def test_ml_states_execute_at_next_session_open(prepared) -> None:  # type: ignore[no-untyped-def]
    _, bars, ds, tgt, per = prepared
    tm = train_model("logistic_regression", CFG, ds, tgt["target"], per)
    states = states_from_probabilities(predict_proba(tm, ds), 0.5)
    res = Backtester(BacktestConfig()).run_states(bars, states, strategy_id="ml")
    f = res.fills
    assert len(f) > 0
    assert (f["ts"] > f["signal_observed_at"]).all()
    np.testing.assert_array_equal(f["price"].to_numpy(), bars.loc[f["ts"], "open"].to_numpy())


# ── report ──


def test_report_structure(report: MLReport) -> None:
    assert {r["period"] for r in report.class_distribution} == {"train", "validation", "test"}
    formal = [c for c in report.comparisons if c["family"] == "formal"]
    assert len(formal) == 12 and {c["family_size"] for c in formal} == {12}
    assert {c["b"] for c in formal} == {
        "benchmark_buy_and_hold_price",
        "sma_trend",
        "sma_crossover",
        "rsi_momentum",
        "price_action_trend",
        "regime_trend",
    }
    fin = pd.DataFrame(report.financial)
    assert set(fin["window"]) == {"test", "validation", "late", "early", "full"}
    assert set(fin["scenario"]) == {"A_zero", "B_5bps", "C_10bps", "D_default"}
    assert set(fin.loc[fin["window"] == "early", "role"]) == {"in_sample_diagnostic"}
    assert len(report.verdicts) == 2
    assert all(
        v["verdict"] in ("INCREMENTAL EDGE", "NO INCREMENTAL EDGE FOUND") for v in report.verdicts
    )
    assert report.sample_counts["no_target_last_row"] == 1
    roles = {r["period"]: r["role"] for r in report.classification}
    assert roles["test"] == "out-of-sample PRIMARY" and "diagnostic" in roles["train"]


def test_report_is_deterministic(prepared, report: MLReport) -> None:  # type: ignore[no-untyped-def]
    exp, bars, *_ = prepared
    again = exp.run(bars)
    for name in ("classification", "financial", "comparisons", "auc_intervals", "verdicts"):
        pd.testing.assert_frame_equal(
            pd.DataFrame(getattr(report, name)), pd.DataFrame(getattr(again, name))
        )
    assert again.experiment_id == report.experiment_id


def test_costs_never_change_ml_orders(report: MLReport) -> None:
    fin = pd.DataFrame(report.financial)
    ml = fin[fin["subject"].str.startswith("ml_")]
    assert (ml.groupby(["window", "subject"])["orders"].nunique() == 1).all()


def test_edge_rule_requires_all_conditions(report: MLReport) -> None:
    exp = MLExperiment(
        dataclasses.replace(CFG, edge_rule=EdgeRule(alpha=1.01, auc_floor=-1.0)),
        validation_config=VCFG,
    )
    fake = dataclasses.replace(report)
    fake.comparisons = [{**c, "estimate": abs(c["estimate"]) + 1e-9} for c in report.comparisons]
    fin = pd.DataFrame(report.financial)
    big = (fin["window"] == "test") & fin["subject"].str.startswith("ml_")
    fin.loc[big, "sharpe"] = 99.0
    fake.financial = fin.to_dict("records")
    assert {v["verdict"] for v in exp._verdicts(fake, ["ml_logistic_regression"])} == {
        "INCREMENTAL EDGE"
    }
    # Same favourable fake, but an unattainable AUC floor: one failed condition -> no edge.
    no_auc = MLExperiment(
        dataclasses.replace(CFG, edge_rule=EdgeRule(alpha=1.01, auc_floor=2.0)),
        validation_config=VCFG,
    )
    verdict = no_auc._verdicts(fake, ["ml_logistic_regression"])[0]
    assert (
        verdict["verdict"] == "NO INCREMENTAL EDGE FOUND" and not verdict["auc_lower_above_floor"]
    )


def test_write_outputs(report: MLReport, tmp_path: Path) -> None:
    out = report.write(tmp_path)
    for name in (
        "experiment.json",
        "classification.csv",
        "financial.csv",
        "comparisons.csv",
        "class_distribution.csv",
        "auc_intervals.csv",
        "verdicts.csv",
        "model_parameters.csv",
    ):
        assert (out / name).exists()


def test_config_validation() -> None:
    with pytest.raises(ValueError):
        MLConfig(models=("deep_net",))
    with pytest.raises(ValueError):
        MLConfig(decision_threshold=1.0)
    assert MLConfig().fingerprint() == MLConfig().fingerprint()
    assert MLConfig(seed=1).fingerprint() != MLConfig().fingerprint()
