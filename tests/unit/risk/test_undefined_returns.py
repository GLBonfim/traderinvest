"""ADR-0034 step 4b: undefined daily returns (previous equity 0) and `risk-daily-returns/2`.

An undefined return is `pd.NA` in a nullable `Float64` column, `null` in the artifact, never 0,
NaN, ±inf or an estimate; a close that reaches 0 is a defined -1. Metrics and comparisons that
would need it are reported as undefined / not testable; nothing is dropped or imputed. Schema
/1 artifacts keep their contract. Synthetic data, fake suites and `tmp_path` only.
"""

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.backtest.metrics import (
    UNDEFINED_RETURNS_NOTE,
    defined_returns,
    returns_from_equity,
    undefined_mask,
    without_undefined_return_metrics,
)
from app.paper.config import PaperConfig
from app.paper.engine import PaperTradingEngine
from app.risk import artifacts as art
from app.risk import diagnostics as dg
from app.risk.diagnostics import METRIC_KEYS, RiskSuiteReport
from app.risk.engine import RiskRun
from app.validation.config import ValidationConfig
from tests.unit.risk.test_adr0034_baseline import L
from tests.unit.risk.test_da5_invalid_state import BLOCK3, total_loss_bars
from tests.unit.risk.test_overlay import CAL, FREE, flat_indicators, run
from tests.unit.risk.test_research_artifacts import (
    GridSuite,
    _fixed_code,  # noqa: F401 (autouse fixture: fixed code version in manifests)
    manifest,
    rewrite_manifest,
    run_grid,
    snapshot,
)
from tests.unit.risk.test_research_cli import fake_equity

INITIAL = 100_000.0


def series(values: list[float]) -> pd.Series:
    return pd.Series(values, index=pd.date_range("2024-01-02", periods=len(values), tz="UTC"))


# ── definition ──


def test_previous_equity_zero_gives_undefined_and_reaching_zero_gives_minus_one() -> None:
    r = defined_returns(series([110_000.0, 0.0, 0.0, 50.0]), INITIAL)
    assert str(r.dtype) == "Float64"
    assert r.iloc[0] == 110_000.0 / INITIAL - 1
    assert r.iloc[1] == -1.0  # positive -> 0: defined
    assert r.isna().tolist() == [False, False, True, True]  # 0 -> 0 and 0 -> 50: undefined
    assert undefined_mask(r).tolist() == [False, False, True, True]


def test_finite_returns_are_bit_identical_to_the_phase8_function() -> None:
    eq = series(list(INITIAL * np.cumprod(1 + np.random.default_rng(3).normal(0, 0.01, 500))))
    new = defined_returns(eq, INITIAL)
    old = returns_from_equity(eq, INITIAL)
    assert not new.isna().any()
    assert [float(x).hex() for x in new] == [float(x).hex() for x in old]


def test_empty_series_and_plain_nan_are_not_undefined() -> None:
    assert len(defined_returns(series([]), INITIAL)) == 0
    plain = pd.Series([0.01, math.nan, math.inf])
    assert not undefined_mask(plain).any()  # a float64 NaN is non-finite, not "undefined"


# ── metrics ──


def test_metrics_without_undefined_returns_are_the_same_object() -> None:
    m = {"sharpe": 1.0}
    assert without_undefined_return_metrics(m, 0, 0) is m


def test_return_based_metrics_become_undefined_with_the_reason() -> None:
    m = {"sharpe": 0.0, "sortino": 0.0, "annualized_volatility": 0.0, "gross_sharpe": 1.0,
         "max_drawdown": -1.0, "cumulative_return": -1.0}  # fmt: skip
    out = without_undefined_return_metrics(m, 3, 0)
    assert (out["sharpe"], out["sortino"], out["annualized_volatility"]) == (None, None, None)
    assert out["gross_sharpe"] == 1.0 and out["max_drawdown"] == -1.0  # equity-based: kept
    assert out["undefined_returns"] == 3
    assert out["return_metrics_note"] == UNDEFINED_RETURNS_NOTE.format(n=3)
    assert m["sharpe"] == 0.0  # the input is not modified
    gross = without_undefined_return_metrics(m, 0, 2)
    assert gross["gross_sharpe"] is None and gross["sharpe"] == 0.0


@pytest.mark.parametrize("engine", ["research", "paper"])
def test_engines_record_undefined_returns_and_undefined_metrics(engine: str) -> None:
    bars = total_loss_bars()
    states = [L] * len(bars)
    if engine == "research":
        r = run(bars, states, BLOCK3)
        eq, metrics = r.equity, r.metrics
    else:
        from app.backtest.engine import session_times

        p = PaperTradingEngine(PaperConfig(risk=BLOCK3, backtest=FREE)).replay(
            bars,
            pd.Series(states, index=bars.index),
            flat_indicators(bars),
            session_times(pd.DatetimeIndex(bars.index), CAL),
            strategy_id="x",
        )
        eq, metrics = p.equity, p.metrics
    ret = eq["daily_return"]
    zero = int((eq["equity"] == 0.0).sum())
    assert zero == 8
    assert ret[eq["equity"] == 0.0].iloc[0] == -1.0  # the close that reaches 0
    assert int(undefined_mask(ret).sum()) == zero - 1  # every later session: undefined
    assert metrics["sharpe"] is None and metrics["undefined_returns"] == zero - 1
    assert "not computed" in metrics["return_metrics_note"]


# ── comparisons ──


def report_with(undefined_in: str | None, n: int = 260) -> RiskSuiteReport:
    rep = RiskSuiteReport("1.0.0", "bt", {})
    idx = pd.date_range("2020-01-02", periods=n, tz="UTC")
    for i, sc in enumerate(("control_no_overlay", "drawdown_lock_20", "atr_stop_3")):
        r = list(np.random.default_rng(i).normal(0.0004, 0.01, n))
        eq = fake_equity(idx, r)
        if sc == undefined_in:
            eq.loc[eq.index[100:], "equity"] = 0.0
        eq["daily_return"] = defined_returns(eq["equity"], INITIAL)
        empty = pd.DataFrame()
        rep.runs[("full", "s", sc)] = RiskRun("s", sc, empty, eq, empty, empty, None, None,
                                              {k: 0.0 for k in METRIC_KEYS}, {})  # fmt: skip
    return rep


NAMES = ["control_no_overlay", "drawdown_lock_20", "atr_stop_3"]
V = ValidationConfig(n_resamples=200)


def test_an_undefined_overlay_series_makes_only_its_comparisons_not_testable() -> None:
    rows = dg._compare(report_with("drawdown_lock_20"), "full", NAMES, V)
    assert len(rows) == 2 * len(dg.COMPARED_METRICS)  # every row is still reported
    dd = [r for r in rows if r["scenario"] == "drawdown_lock_20"]
    assert all(r["note"].startswith("not testable") and "drawdown_lock_20" in r["note"] for r in dd)
    assert all(math.isnan(r["estimate"]) and r["n_finite"] == 0 for r in dd)
    assert all(r["estimate_in_interval"] is None for r in dd)
    atr = [r for r in rows if r["scenario"] == "atr_stop_3"]
    ref = dg._compare(report_with(None), "full", ["control_no_overlay", "atr_stop_3"], V)
    assert [(r["estimate"], r["lower"], r["upper"]) for r in atr] == [
        (r["estimate"], r["lower"], r["upper"]) for r in ref
    ]  # computed on the full, untouched sessions of the testable series


def test_an_undefined_control_makes_every_comparison_not_testable() -> None:
    rows = dg._compare(report_with("control_no_overlay"), "full", NAMES, V)
    assert len(rows) == 2 * len(dg.COMPARED_METRICS)
    assert all(r["note"].startswith("not testable") for r in rows)
    assert all("control_no_overlay" in r["note"] for r in rows)
    assert not any(r["estimate"] == 0.0 for r in rows)  # undefined is never reported as 0


# ── artifact schema /2 ──


class ZeroSuite(GridSuite):
    """GridSuite where (full, sma_trend, atr_stop_3) loses everything at session 2."""

    def __call__(self, bars: pd.DataFrame, **kw: Any) -> RiskSuiteReport:
        report = super().__call__(bars, **kw)
        key = ("full", "sma_trend", "atr_stop_3")
        r = report.runs[key]
        eq = r.equity.copy()
        eq.loc[eq.index[2:], "equity"] = 0.0
        eq["daily_return"] = defined_returns(eq["equity"], kw["backtest"].initial_capital)
        report.runs[key] = RiskRun(r.strategy_id, r.scenario, r.decisions, eq, r.fills, r.trades,
                                   None, None, r.metrics, r.diagnostics, r.risk_fingerprint,
                                   r.backtest_fingerprint)  # fmt: skip
        return report


def returns_lines(out: Path) -> list[str]:
    return (out / art.RETURNS_FILE).read_text(encoding="utf-8").splitlines()


def test_new_runs_write_schema_2_with_explicit_nulls(tmp_path: Path) -> None:
    rc, out = run_grid(tmp_path, suite=ZeroSuite())
    assert rc == 0
    block = manifest(out)["results"]["daily_returns"]
    assert block["schema"] == art.RETURNS_SCHEMA == "risk-daily-returns/2"
    assert block["convention"] == art.RETURN_CONVENTION
    counts = {(s["slice"], s["strategy"], s["scenario"]): s["undefined_returns"]
              for s in block["series"]}  # fmt: skip
    assert counts[("full", "sma_trend", "atr_stop_3")] == 3  # sessions 3..5 (6 bars)
    assert sum(counts.values()) == 3
    rows = [ln.split(",") for ln in returns_lines(out)[1:]]
    nulls = [r for r in rows if r[7] == art.NULL]
    assert len(nulls) == 3 and {tuple(r[:3]) for r in nulls} == {
        ("full", "sma_trend", "atr_stop_3")
    }
    reached = [r for r in rows if r[:3] == ["full", "sma_trend", "atr_stop_3"] and r[4] == "2"]
    assert reached[0][7] == "-1.0"  # reaching 0 is a defined -1
    assert art.verify_run(out).ok


def test_a_run_without_undefined_returns_writes_zero_counts(tmp_path: Path) -> None:
    _, out = run_grid(tmp_path)
    series_ = manifest(out)["results"]["daily_returns"]["series"]
    assert all(s["undefined_returns"] == 0 for s in series_)
    assert not any(art.NULL in ln for ln in returns_lines(out))


def test_parse_keeps_null_distinct_from_zero_and_nan(tmp_path: Path) -> None:
    _, out = run_grid(tmp_path, suite=ZeroSuite())
    df = art.read_returns(out / art.RETURNS_FILE)
    assert str(df["daily_return"].dtype) == "Float64"
    assert int(undefined_mask(df["daily_return"]).sum()) == 3
    (tmp_path / "p").mkdir()
    rc, plain = run_grid(tmp_path / "p")
    assert rc == 0
    assert str(art.read_returns(plain / art.RETURNS_FILE)["daily_return"].dtype) == "float64"


def edit_returns(out: Path, edit: Any) -> None:
    """Rewrites the returns file through `edit(rows)` and re-hashes it consistently."""
    lines = returns_lines(out)
    rows = [ln.split(",") for ln in lines[1:]]
    edit(rows)
    text = "\n".join([lines[0], *(",".join(r) for r in rows)]) + "\n"
    (out / art.RETURNS_FILE).write_text(text, encoding="utf-8", newline="\n")
    m = manifest(out)
    import hashlib

    for a in m["artifacts"]:
        if a["path"] == art.RETURNS_FILE:
            a["sha256"] = hashlib.sha256(text.encode()).hexdigest()
            a["bytes"] = len(text.encode())
    rewrite_manifest(out, m)


def _key(r: list[str], session: int) -> bool:
    return r[:3] == ["full", "sma_trend", "atr_stop_3"] and r[4] == str(session)


@pytest.mark.parametrize(
    ("name", "edit", "problem"),
    [
        ("null where previous equity > 0",
         lambda rows: next(r for r in rows if _key(r, 1)).__setitem__(7, art.NULL),
         "non-zero previous equity"),
        ("number where previous equity = 0",
         lambda rows: next(r for r in rows if _key(r, 4)).__setitem__(7, "0.0"),
         "previous equity is 0"),
    ],
)  # fmt: skip
def test_verifier_accepts_nulls_only_after_a_zero_equity(
    tmp_path: Path, name: str, edit: Any, problem: str
) -> None:
    _, out = run_grid(tmp_path, suite=ZeroSuite())
    edit_returns(out, edit)
    rep = art.verify_run(out)
    assert not rep.ok and any(problem in p for p in rep.problems), (name, rep.problems)


def test_verifier_checks_the_undefined_count_of_the_manifest(tmp_path: Path) -> None:
    _, out = run_grid(tmp_path, suite=ZeroSuite())
    m = manifest(out)
    for s in m["results"]["daily_returns"]["series"]:
        if s["scenario"] == "atr_stop_3" and s["strategy"] == "sma_trend" and s["slice"] == "full":
            s["undefined_returns"] = 1
    rewrite_manifest(out, m)
    rep = art.verify_run(out)
    assert not rep.ok and any("undefined returns differ" in p for p in rep.problems)
    del m["results"]["daily_returns"]["series"][0]["undefined_returns"]
    rewrite_manifest(out, m)
    assert any("undefined_returns: missing" in p for p in art.verify_run(out).problems)


@pytest.mark.parametrize("token", ["NULL", "None", "NA", "<NA>", "", "nul", "NaN", "null "])
def test_invalid_markers_are_refused(tmp_path: Path, token: str) -> None:
    _, out = run_grid(tmp_path, suite=ZeroSuite())
    edit_returns(out, lambda rows: next(r for r in rows if _key(r, 4)).__setitem__(7, token))
    assert not art.verify_run(out).ok


def test_null_tokens_elsewhere_are_refused() -> None:
    header = ",".join(art.RETURNS_COLUMNS)
    row = "full,s,x,B,0,2024-01-02T14:30:00+00:00,null,0.0,1.0,0.0"
    with pytest.raises(ValueError, match="not a number"):
        art.parse_returns(f"{header}\n{row}\n".encode())


# ── loading ──


def test_matrix_refuses_a_series_with_undefined_returns(tmp_path: Path) -> None:
    _, out = run_grid(tmp_path, suite=ZeroSuite())
    before = snapshot(out)
    with pytest.raises(art.ReturnsMatrixError, match="3 undefined daily return"):
        art.load_returns_matrix(out, slice_name="full", strategy="sma_trend",
                                scenarios=["control_no_overlay", "atr_stop_3"])  # fmt: skip
    mat = art.load_returns_matrix(out, slice_name="full", strategy="buy_and_hold",
                                  scenarios=["control_no_overlay", "atr_stop_3"])  # fmt: skip
    assert mat.dtypes.tolist() == [np.dtype("float64")] * 2  # complete series load as before
    assert snapshot(out) == before


# ── schema /1 stays valid under its own contract ──


def as_returns_v1(out: Path) -> None:
    """The manifest a pre-4b run recorded: schema /1, its convention, no undefined counts."""
    m = manifest(out)
    block = m["results"]["daily_returns"]
    block["schema"], block["convention"] = art.RETURNS_SCHEMA_V1, art.RETURN_CONVENTION_V1
    for s in block["series"]:
        del s["undefined_returns"]
    rewrite_manifest(out, m)


def test_schema_1_runs_verify_and_load_as_before(tmp_path: Path) -> None:
    _, out = run_grid(tmp_path)
    as_returns_v1(out)
    rep = art.verify_run(out)
    assert rep.ok, rep.problems
    mat = art.load_returns_matrix(out, slice_name="full", strategy="sma_trend",
                                  scenarios=["control_no_overlay", "atr_stop_3"])  # fmt: skip
    assert mat.dtypes.tolist() == [np.dtype("float64")] * 2


def test_schema_1_has_no_null_token(tmp_path: Path) -> None:
    _, out = run_grid(tmp_path, suite=ZeroSuite())
    as_returns_v1(out)
    rep = art.verify_run(out)
    assert not rep.ok and any("not part of schema risk-daily-returns/1" in p for p in rep.problems)


def test_schema_1_with_the_schema_2_convention_is_refused(tmp_path: Path) -> None:
    _, out = run_grid(tmp_path)
    as_returns_v1(out)
    m = manifest(out)
    m["results"]["daily_returns"]["convention"] = art.RETURN_CONVENTION
    rewrite_manifest(out, m)
    assert any("convention" in p for p in art.verify_run(out).problems)


def test_manifest_json_keeps_null_counts_as_integers(tmp_path: Path) -> None:
    _, out = run_grid(tmp_path, suite=ZeroSuite())
    raw = json.loads((out / art.MANIFEST).read_text(encoding="utf-8"))
    assert all(isinstance(s["undefined_returns"], int)
               for s in raw["results"]["daily_returns"]["series"])  # fmt: skip
