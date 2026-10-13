"""Daily net-return artifacts and the read-only run verifier.

Fake suite runs with synthetic equity curves only: no backtest, no database."""

import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.risk import artifacts as art
from app.risk import cli, research
from app.risk.diagnostics import METRIC_KEYS, RiskSuiteReport
from app.risk.engine import RiskRun
from tests.unit.risk.test_research_cli import fake_equity, make_bars

SEL_ARGS = [
    "--strategy", "buy_and_hold", "sma_trend",
    "--scenario", "control_no_overlay", "atr_stop_3",
    "--cost", "B",
    "--slice", "full", "early",
]  # fmt: skip


def risk_run(index: pd.Index, returns: list[float], scenario: str = "x",
             risk_fp: str = "riskfp", bt_fp: str = "btfp") -> RiskRun:  # fmt: skip
    empty = pd.DataFrame()
    return RiskRun("s", scenario, empty, fake_equity(index, returns), empty, empty, None, None,
                   {k: 0.0 for k in METRIC_KEYS}, {}, risk_fp, bt_fp)  # fmt: skip


class GridSuite:
    """Exactly the requested runs; every series is distinct (seeded by its key); `early`
    simulates fewer sessions than `full`."""

    def __call__(self, bars: pd.DataFrame, **kw: Any) -> RiskSuiteReport:
        report = RiskSuiteReport("1.0.0", kw["backtest"].fingerprint(), {})
        for sl in kw["slices"]:
            n = 3 if sl.name == "early" else len(bars)
            for sid in [s.strategy_id for s in kw["strategy_engine"].strategies]:
                for sc in kw["scenarios"]:
                    seed = int(hashlib.sha256(f"{sl.name}{sid}{sc.name}".encode()).hexdigest(), 16)
                    r = np.random.default_rng(seed % 2**32).normal(0.0003, 0.011, n)
                    # the fingerprints the engine attaches: the scenario's and the
                    # slice-specific backtest configuration's (as run_risk_suite builds it)
                    bt = dataclasses.replace(
                        kw["backtest"], start=sl.start, end=sl.end, period_label=sl.name
                    )
                    report.runs[(sl.name, sid, sc.name)] = risk_run(
                        bars.index[:n], list(r), sc.name, sc.fingerprint(), bt.fingerprint()
                    )
        return report


@pytest.fixture(autouse=True)
def _fixed_code(monkeypatch: pytest.MonkeyPatch) -> None:
    code = {"app_version": "0", "git_commit": "c" * 40, "git_branch": "b", "git_dirty": False}
    monkeypatch.setattr(research, "code_info", lambda: dict(code))


def run_grid(tmp_path: Path, args: list[str] | None = None, suite: Any = None) -> tuple[int, Path]:
    out = tmp_path / "r"
    argv = ["run", "--symbol", "SPY", "--out", str(out), *(args or SEL_ARGS), "--no-compare"]
    rc = cli.main(argv, load_bars=make_bars, run_suite=suite or GridSuite(), configure=False)
    return rc, out


def manifest(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((path / art.MANIFEST).read_text(encoding="utf-8"))
    return data


def rewrite_manifest(path: Path, m: dict[str, Any]) -> None:
    (path / art.MANIFEST).write_text(json.dumps(m), encoding="utf-8")


def snapshot(path: Path) -> dict[str, tuple[int, bytes]]:
    return {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in sorted(path.iterdir())}


# ── values: lossless and explicit ──


def test_float_text_round_trips_exactly() -> None:
    rng = np.random.default_rng(0)
    values = [*rng.normal(0, 0.02, 1000), 1e-300, -1e300, 5e-324, 0.1, -0.0, 0.0]
    for v in values:
        assert art.parse(art.fmt(v)) == v
        assert math.copysign(1, art.parse(art.fmt(v))) == math.copysign(1, v)


def test_non_finite_values_are_explicit_tokens() -> None:
    assert [art.fmt(x) for x in (math.nan, math.inf, -math.inf)] == ["nan", "inf", "-inf"]
    assert math.isnan(art.parse("nan")) and art.parse("-inf") == -math.inf
    for bad in ("NaN", "Infinity", "1e400", "", "abc"):
        with pytest.raises(ValueError):
            art.parse(bad)


# ── building the returns file ──


def test_rows_ordering_identifiers_and_sessions() -> None:
    idx = pd.date_range("2024-01-02 14:30", periods=4, freq="D", tz="America/New_York")
    runs = [(("full", "a", "control_no_overlay"), risk_run(idx, [0.0, 0.01, -0.02, 0.005])),
            (("full", "a", "atr_stop_3"), risk_run(idx[:2], [0.0, math.nan]))]  # fmt: skip
    text, infos = art.build_returns(runs, "D_default")
    lines = text.splitlines()
    assert lines[0] == ",".join(art.RETURNS_COLUMNS)
    assert len(lines) == 1 + 4 + 2
    first = lines[1].split(",")
    assert first[:5] == ["full", "a", "control_no_overlay", "D_default", "0"]
    assert first[5] == idx[0].tz_convert("UTC").isoformat()  # UTC, ISO-8601
    assert lines[-1].split(",")[7] == "nan"  # written explicitly, never imputed
    assert [i.rows for i in infos] == [4, 2] and infos[1].non_finite_returns == 1
    assert infos[0].first_ts == idx[0].tz_convert("UTC").isoformat()
    assert infos[0].risk_fingerprint == "riskfp" and infos[0].cost_scenario == "D_default"


@pytest.mark.parametrize("problem", ["duplicate_key", "missing_column", "unsorted", "naive"])
def test_ambiguous_series_are_refused(problem: str) -> None:
    idx = pd.date_range("2024-01-02", periods=3, freq="D", tz="UTC")
    run = risk_run(idx, [0.0, 0.01, 0.02])
    runs = [(("full", "a", "c"), run)]
    if problem == "duplicate_key":
        runs.append((("full", "a", "c"), run))
    elif problem == "missing_column":
        run.equity = run.equity.drop(columns=["costs"])
    elif problem == "unsorted":
        run.equity = run.equity.iloc[::-1]
    else:
        run.equity.index = idx.tz_localize(None)
    with pytest.raises(ValueError):
        art.build_returns(runs, "D_default")


def test_atomic_write_never_overwrites_and_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = tmp_path / art.RETURNS_FILE
    art.write_new_atomic(p, "first")
    with pytest.raises(FileExistsError):
        art.write_new_atomic(p, "second")
    assert p.read_text(encoding="utf-8") == "first"
    assert sorted(x.name for x in tmp_path.iterdir()) == [art.RETURNS_FILE]

    def no_link(src: Any, dst: Any) -> None:
        raise OSError("link failed")

    q = tmp_path / "other.csv"
    monkeypatch.setattr(os, "link", no_link)
    with pytest.raises(OSError):
        art.write_new_atomic(q, "x")
    assert not q.exists() and not (tmp_path / "other.csv.tmp").exists()


# ── a run: artifact, manifest, separation, precision ──


def test_run_writes_returns_listed_and_hashed_in_a_v3_manifest(tmp_path: Path) -> None:
    rc, out = run_grid(tmp_path)
    assert rc == cli.EXIT_OK
    m = manifest(out)
    assert m["schema_version"] == "risk-research-run/3" and m["status"] == research.COMPLETE
    for key in ("environment", "statistics", "db_access", "selection", "config", "code"):
        assert key in m  # v2 content is preserved
    block = m["results"]["daily_returns"]
    assert block["path"] == art.RETURNS_FILE and block["schema"] == art.RETURNS_SCHEMA
    assert block["columns"] == list(art.RETURNS_COLUMNS)
    assert block["convention"] == art.RETURN_CONVENTION
    assert block["cost_scenario"] == "B_5bps" and block["initial_capital"] == 100_000.0
    data = (out / art.RETURNS_FILE).read_bytes()
    (entry,) = [a for a in m["artifacts"] if a["path"] == art.RETURNS_FILE]
    assert entry["sha256"] == hashlib.sha256(data).hexdigest() and entry["bytes"] == len(data)
    # 2 slices x 2 strategies x 2 scenarios, canonical order, early shorter than full
    keys = [(s["slice"], s["strategy"], s["scenario"]) for s in block["series"]]
    assert keys == [(sl, st, sc) for sl in ("full", "early") for st in ("buy_and_hold", "sma_trend")
                    for sc in ("control_no_overlay", "atr_stop_3")]  # fmt: skip
    assert {s["rows"] for s in block["series"] if s["slice"] == "early"} == {3}
    assert {s["rows"] for s in block["series"] if s["slice"] == "full"} == {6}
    assert block["rows"] == 4 * 3 + 4 * 6
    assert not list(out.glob("*.tmp"))


def test_persisted_returns_are_exactly_the_suite_series(tmp_path: Path) -> None:
    suite = GridSuite()
    captured: dict[tuple[str, str, str], RiskRun] = {}

    def spy(bars: pd.DataFrame, **kw: Any) -> RiskSuiteReport:
        report = suite(bars, **kw)
        captured.update(report.runs)
        return report

    rc, out = run_grid(tmp_path, suite=spy)
    assert rc == cli.EXIT_OK
    df = art.read_returns(out / art.RETURNS_FILE)
    for key, run in captured.items():
        part = df[(df["slice"] == key[0]) & (df["strategy"] == key[1]) & (df["scenario"] == key[2])]
        for c in art.SERIES_COLUMNS:  # bit-for-bit, no mixing between series
            np.testing.assert_array_equal(part[c].to_numpy(), run.equity[c].to_numpy())
        assert list(pd.DatetimeIndex(part["ts"])) == list(run.equity.index)


def test_returns_write_failure_fails_the_run_without_a_returns_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(path: Path, text: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(art, "write_new_atomic", broken)
    rc, out = run_grid(tmp_path)
    assert rc == cli.EXIT_FAILED
    assert manifest(out)["status"] == research.FAILED
    assert not (out / art.RETURNS_FILE).exists()
    assert not art.verify_run(out).ok


def test_existing_output_directory_is_still_refused(tmp_path: Path) -> None:
    (tmp_path / "r").mkdir()
    rc, out = run_grid(tmp_path)
    assert rc == cli.EXIT_OUTPUT_EXISTS and list(out.iterdir()) == []


# ── read-only verification ──


def test_verify_accepts_a_complete_run_and_writes_nothing(tmp_path: Path) -> None:
    _, out = run_grid(tmp_path)
    before = snapshot(out)
    rep = art.verify_run(out)
    assert rep.ok and rep.has_returns and rep.schema == "risk-research-run/3"
    assert sorted(rep.checked_artifacts) == sorted([research.TABLE, art.RETURNS_FILE])
    assert snapshot(out) == before


def test_verify_detects_a_modified_byte(tmp_path: Path) -> None:
    _, out = run_grid(tmp_path)
    p = out / art.RETURNS_FILE
    data = bytearray(p.read_bytes())
    data[-3] = ord("9") if data[-3] != ord("9") else ord("8")
    p.write_bytes(bytes(data))
    assert any("SHA-256 mismatch" in x for x in art.verify_run(out).problems)


def test_verify_detects_a_consistent_rehash_of_altered_returns(tmp_path: Path) -> None:
    """Even with the manifest hash updated, a return that no longer matches equity is caught."""
    _, out = run_grid(tmp_path)
    p = out / art.RETURNS_FILE
    lines = p.read_text(encoding="utf-8").splitlines()
    f = lines[3].split(",")
    f[7] = art.fmt(art.parse(f[7]) + 0.001)
    lines[3] = ",".join(f)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    m = manifest(out)
    for a in m["artifacts"]:
        if a["path"] == art.RETURNS_FILE:
            a["sha256"], a["bytes"] = hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size
    rewrite_manifest(out, m)
    assert any("inconsistent with equity" in x for x in art.verify_run(out).problems)


@pytest.mark.parametrize("problem", ["missing", "extra", "duplicate_entry", "failed", "rows",
                                     "series", "reordered", "foreign_cost"])  # fmt: skip
def test_verify_reports_structural_problems(tmp_path: Path, problem: str) -> None:
    _, out = run_grid(tmp_path)
    m = manifest(out)
    p = out / art.RETURNS_FILE
    if problem == "missing":
        p.unlink()
    elif problem == "extra":
        (out / "stray.csv.tmp").write_text("x", encoding="utf-8")
    elif problem == "duplicate_entry":
        m["artifacts"].append(dict(m["artifacts"][0]))
    elif problem == "failed":
        m["status"] = "failed"
    elif problem == "rows":
        m["results"]["daily_returns"]["series"][0]["rows"] += 1
    elif problem == "series":
        m["results"]["daily_returns"]["series"].pop()
    else:
        lines = p.read_text(encoding="utf-8").splitlines()
        if problem == "reordered":
            lines[1], lines[2] = lines[2], lines[1]
        else:
            lines[1] = lines[1].replace("B_5bps", "A_zero")
        p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        for a in m["artifacts"]:
            if a["path"] == art.RETURNS_FILE:
                a["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
                a["bytes"] = p.stat().st_size
    rewrite_manifest(out, m)
    rep = art.verify_run(out)
    assert not rep.ok, problem


def as_historical(out: Path, schema: str) -> None:
    """Turns a generated run into the shape an earlier schema version recorded: no returns
    artifact (v1, v2) and no environment/statistics blocks (v1)."""
    m = manifest(out)
    (out / art.RETURNS_FILE).unlink()
    m["artifacts"] = [a for a in m["artifacts"] if a["path"] != art.RETURNS_FILE]
    del m["results"]["daily_returns"]
    if schema == art.V1:
        del m["environment"], m["statistics"]
    m["schema_version"] = schema
    rewrite_manifest(out, m)


@pytest.mark.parametrize("schema", ["risk-research-run/1", "risk-research-run/2"])
def test_verify_accepts_earlier_manifests_with_their_historical_shape(
    tmp_path: Path, schema: str
) -> None:
    _, out = run_grid(tmp_path)
    as_historical(out, schema)
    rep = art.verify_run(out)
    assert rep.ok and not rep.has_returns and rep.schema == schema, rep.problems


def test_minimal_or_unknown_manifests_are_refused(tmp_path: Path) -> None:
    out = tmp_path / "old"
    out.mkdir()
    table = b"slice,strategy,scenario\nfull,sma_trend,control_no_overlay\n"
    (out / "risk_table.csv").write_bytes(table)
    for schema in ("risk-research-run/1", "risk-research-run/2"):
        rewrite_manifest(out, {
            "schema_version": schema, "status": "complete", "results": {"runs": 1},
            "artifacts": [{"path": "risk_table.csv", "bytes": len(table),
                           "sha256": hashlib.sha256(table).hexdigest()}],
        })  # fmt: skip
        rep = art.verify_run(out)
        assert not rep.ok and any("selection" in x for x in rep.problems)
    rewrite_manifest(out, {"schema_version": "risk-research-run/9", "status": "complete"})
    assert not art.verify_run(out).ok


def test_verify_cli_is_read_only_and_needs_no_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out = run_grid(tmp_path)
    capsys.readouterr()  # discard the run's own output
    before = snapshot(out)

    def no_settings() -> None:
        raise AssertionError("verify must not read the settings")

    monkeypatch.setattr("app.core.config.get_settings", no_settings)
    assert cli.main(["verify", "--run", str(out)]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["ok"] is True
    (out / art.RETURNS_FILE).write_text("tampered", encoding="utf-8")
    before[art.RETURNS_FILE] = snapshot(out)[art.RETURNS_FILE]
    assert cli.main(["verify", "--run", str(out)]) == cli.EXIT_FAILED
    assert snapshot(out) == before


# ── consumer interface for later statistics ──


def test_returns_matrix_from_a_verified_run(tmp_path: Path) -> None:
    _, out = run_grid(tmp_path)
    mat = art.load_returns_matrix(out, slice_name="full", strategy="sma_trend",
                                  scenarios=["control_no_overlay", "atr_stop_3"])  # fmt: skip
    assert list(mat.columns) == ["control_no_overlay", "atr_stop_3"] and len(mat) == 6
    idx = pd.DatetimeIndex(mat.index)
    assert idx.is_monotonic_increasing and str(idx.tz) == "UTC"
    with pytest.raises(art.ReturnsMatrixError, match="no series"):
        art.load_returns_matrix(out, slice_name="late", strategy="sma_trend",
                                scenarios=["control_no_overlay"])  # fmt: skip
    (out / art.RETURNS_FILE).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="does not verify"):
        art.load_returns_matrix(out, slice_name="full", strategy="sma_trend",
                                scenarios=["control_no_overlay"])  # fmt: skip
