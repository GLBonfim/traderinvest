"""Phase 10 on the real SPY history with the DEFAULT configuration (read-only).

Asserts structure, timing and reproducibility only — no model outcome is encoded.
"""

from collections.abc import Iterator

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.backtest.data import load_backtest_bars
from app.core.config import Settings
from app.ml import MLExperiment
from app.ml.training import predict_proba, train_model

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def spy() -> Iterator[pd.DataFrame]:
    try:
        engine = create_engine(Settings().database_url(), connect_args={"connect_timeout": 3})  # type: ignore[call-arg]
        with Session(engine) as session:
            _, bars = load_backtest_bars(session, "SPY")
    except (OperationalError, LookupError) as exc:
        pytest.skip(f"SPY data unavailable: {type(exc).__name__}")
    if len(bars) < 8000:
        pytest.skip("full SPY history required")
    yield bars
    engine.dispose()


def test_full_spy_experiment(spy: pd.DataFrame) -> None:
    before = spy.copy(deep=True)
    exp = MLExperiment()
    report = exp.run(spy)
    pd.testing.assert_frame_equal(spy, before)

    counts = report.sample_counts
    assert counts["train"] + counts["validation"] + counts["test"] + counts["purged"] + counts[
        "insufficient_features"
    ] + counts["no_target_last_row"] == len(spy)
    assert counts["insufficient_features"] == 272 and counts["purged"] == 2
    dist = {r["period"]: r for r in report.class_distribution}
    assert pd.Timestamp(dist["train"]["last_session"]).date() <= exp.config.split.train_end
    assert pd.Timestamp(dist["test"]["first_session"]).date() >= exp.config.split.test_start
    assert len([c for c in report.comparisons if c["family"] == "formal"]) == 12
    assert {v["model"] for v in report.verdicts} == {"logistic_regression", "random_forest"}
    assert report.overall_verdict in (
        "NO INCREMENTAL EDGE FOUND",
        "INCREMENTAL EDGE (see conditions)",
    )

    # reproducibility on the real data (bit-identical probabilities)
    ds, tgt, per = exp.prepare(spy)
    for m in exp.config.models:
        a = predict_proba(train_model(m, exp.config, ds, tgt["target"], per), ds)
        b = predict_proba(train_model(m, exp.config, ds, tgt["target"], per), ds)
        pd.testing.assert_series_equal(a, b)
