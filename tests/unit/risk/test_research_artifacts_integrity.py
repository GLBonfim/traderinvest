"""Regression tests for the Step 7A review findings (M1-M3, L1-L5) on the risk research
artifacts and their read-only verifier. Synthetic runs in temporary directories only."""

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.risk import artifacts as art
from app.risk import cli, research
from tests.unit.risk.test_research_artifacts import (
    SEL_ARGS,
    GridSuite,
    manifest,
    rewrite_manifest,
    run_grid,
    snapshot,
)
from tests.unit.risk.test_research_cli import make_bars


@pytest.fixture(autouse=True)
def _fixed_code(monkeypatch: pytest.MonkeyPatch) -> None:
    code = {"app_version": "0", "git_commit": "c" * 40, "git_branch": "b", "git_dirty": False}
    monkeypatch.setattr(research, "code_info", lambda: dict(code))


@pytest.fixture
def run(tmp_path: Path) -> Path:
    rc, out = run_grid(tmp_path)
    assert rc == cli.EXIT_OK and art.verify_run(out).ok
    return out


def rehash(out: Path, m: dict[str, Any]) -> None:
    for a in m["artifacts"]:
        p = out / a["path"]
        if p.is_file():
            a["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
            a["bytes"] = p.stat().st_size


def edit_returns_line(out: Path, line_no: int, column: int, value: str) -> None:
    """Rewrites one CSV field and re-hashes the manifest (a coordinated edit)."""
    p = out / art.RETURNS_FILE
    lines = p.read_text(encoding="utf-8").splitlines()
    fields = lines[line_no].split(",")
    fields[column] = value
    lines[line_no] = ",".join(fields)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    m = manifest(out)
    rehash(out, m)
    rewrite_manifest(out, m)


def problems(out: Path) -> list[str]:
    rep = art.verify_run(out)
    assert not rep.ok
    return rep.problems


# ── M1: artifact names ──


@pytest.mark.parametrize(
    "name", ["../outside.csv", "sub/x.csv", "sub\\x.csv", "..", ".hidden", "manifest.json", "ABS"]
)
def test_unsafe_artifact_names_are_refused_before_reading(run: Path, name: str) -> None:
    outside = run.parent / "outside.csv"
    outside.write_text("x", encoding="utf-8")
    target = str(outside.resolve()) if name == "ABS" else name
    m = manifest(run)
    m["artifacts"].append({"path": target, "bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()})
    rewrite_manifest(run, m)
    rep = art.verify_run(run)
    assert not rep.ok and any("refused" in p for p in rep.problems)
    assert target not in rep.checked_artifacts


def test_symbolic_link_artifacts_are_refused(run: Path) -> None:
    outside = run.parent / "outside.csv"
    outside.write_text("x", encoding="utf-8")
    try:
        os.symlink(outside, run / "linked.csv")
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links not permitted for this user")
    m = manifest(run)
    m["artifacts"].append(
        {"path": "linked.csv", "bytes": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
    )
    rewrite_manifest(run, m)
    assert any("symbolic link" in p for p in problems(run))


# ── M2: malformed manifests produce reports, never exceptions ──


MUTATIONS: dict[str, Callable[[Any], Any]] = {
    "not_an_object": lambda m: [],
    "artifacts_dict": lambda m: {**m, "artifacts": {"x": 1}},
    "artifact_string": lambda m: {**m, "artifacts": [*m["artifacts"], "risk_table.csv"]},
    "artifact_bytes_bool": lambda m: {**m, "artifacts": [{**m["artifacts"][0], "bytes": True}]},
    "artifact_bad_sha": lambda m: {**m, "artifacts": [{**m["artifacts"][0], "sha256": "ABC"}]},
    "results_list": lambda m: {**m, "results": []},
    "selection_missing": lambda m: {k: v for k, v in m.items() if k != "selection"},
    "strategies_empty": lambda m: {**m, "selection": {**m["selection"], "strategies": []}},
    "compare_string": lambda m: {**m, "selection": {**m["selection"], "compare": "yes"}},
    "status_unknown": lambda m: {**m, "status": "done"},
    "returns_block_string": lambda m: {**m, "results": {**m["results"], "daily_returns": "x"}},
    "series_entry_without_slice": lambda m: {**m, "results": {**m["results"], "daily_returns": {
        **m["results"]["daily_returns"],
        "series": [{k: v for k, v in s.items() if k != "slice"}
                   for s in m["results"]["daily_returns"]["series"]]}}},
    "config_slices_not_list": lambda m: {**m, "config": {**m["config"], "slices": "full"}},
}  # fmt: skip


@pytest.mark.parametrize("mutation", list(MUTATIONS))
def test_malformed_manifests_are_reported_not_raised(
    run: Path, mutation: str, capsys: pytest.CaptureFixture[str]
) -> None:
    (run / art.MANIFEST).write_text(json.dumps(MUTATIONS[mutation](manifest(run))), "utf-8")
    rep = art.verify_run(run)  # must not raise
    assert not rep.ok and rep.problems
    assert not any(p.startswith("verification error") for p in rep.problems), rep.problems
    capsys.readouterr()
    assert cli.main(["verify", "--run", str(run)]) == cli.EXIT_FAILED
    assert json.loads(capsys.readouterr().out)["ok"] is False
    with pytest.raises(art.ReturnsMatrixError, match="does not verify"):
        art.load_returns_matrix(run, slice_name="full", strategy="sma_trend",
                                scenarios=["control_no_overlay"])  # fmt: skip


@pytest.mark.parametrize("damage", ["invalid_json", "no_manifest", "no_directory"])
def test_unreadable_runs_are_reported(run: Path, damage: str) -> None:
    target = run
    if damage == "invalid_json":
        (run / art.MANIFEST).write_text("{not json", encoding="utf-8")
    elif damage == "no_manifest":
        (run / art.MANIFEST).unlink()
    else:
        target = run.parent / "does-not-exist"
    rep = art.verify_run(target)
    assert not rep.ok and rep.problems


# ── M3: self-consistency, not authenticity ──


def test_guarantee_is_stated_and_a_coordinated_edit_is_not_claimed_detected(run: Path) -> None:
    rep = art.verify_run(run).to_dict()
    assert "Not authenticity" in rep["guarantee"] and "not detected" in rep["guarantee"]
    # documented limitation: equity AND return edited together, manifest re-hashed
    lines = (run / art.RETURNS_FILE).read_text(encoding="utf-8").splitlines()
    prev_eq = art.parse(lines[-2].split(",")[6])
    new_eq = art.parse(lines[-1].split(",")[6]) * 1.05
    edit_returns_line(run, len(lines) - 1, 6, art.fmt(new_eq))
    edit_returns_line(run, len(lines) - 1, 7, art.fmt(new_eq / prev_eq - 1))
    assert art.verify_run(run).ok  # self-consistent: by design not detectable here


# ── L1: historical schema requirements ──


def test_complete_run_cannot_pass_with_no_artifacts(tmp_path: Path) -> None:
    out = tmp_path / "empty"
    out.mkdir()
    m: dict[str, Any] = {
        "schema_version": "risk-research-run/2", "run_id": "r", "status": "complete",
        "selection": {"mode": "selective", "strategies": ["sma_trend"],
                      "risk_scenarios": ["control_no_overlay"], "slices": ["full"],
                      "cost_scenario": "D_default", "compare": False},
        "artifacts": [], "environment": {"numpy_version": "x"}, "statistics": {},
        "results": {"runs": 1, "table_rows": 1, "comparisons": 0, "windows": {},
                    "backtest_fingerprint": "f"},
        "config": {"backtest": {"fingerprint": "f", "config": {}}, "risk_scenarios": {},
                   "slices": [], "cost_scenario": {"name": "D_default"}},
    }  # fmt: skip
    rewrite_manifest(out, m)
    assert any("required artifacts not listed" in p for p in problems(out))


@pytest.mark.parametrize("change", ["drop_comparisons", "extra_artifact", "drop_returns_entry",
                                    "runs_count", "comparisons_count", "table_rows"])  # fmt: skip
def test_required_artifacts_and_counts(tmp_path: Path, change: str) -> None:
    rc, out = run_grid(tmp_path)
    assert rc == cli.EXIT_OK
    m = manifest(out)
    if change == "drop_comparisons":  # a compare run without its comparisons file
        m["selection"]["compare"] = True
    elif change == "extra_artifact":
        (out / "notes.txt").write_text("n", encoding="utf-8")
        m["artifacts"].append({"path": "notes.txt", "bytes": 1,
                               "sha256": hashlib.sha256(b"n").hexdigest()})  # fmt: skip
    elif change == "drop_returns_entry":
        m["artifacts"] = [a for a in m["artifacts"] if a["path"] != art.RETURNS_FILE]
        (out / art.RETURNS_FILE).unlink()
    elif change == "runs_count":
        m["results"]["runs"] += 1
    elif change == "comparisons_count":
        m["results"]["comparisons"] = 3
    else:
        lines = (out / research.TABLE).read_text(encoding="utf-8").splitlines()
        (out / research.TABLE).write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
        rehash(out, m)
    rewrite_manifest(out, m)
    assert problems(out)


def test_compare_run_requires_and_checks_the_comparisons_file(tmp_path: Path) -> None:
    out = tmp_path / "c"
    rc = cli.main(["run", "--symbol", "SPY", "--out", str(out), *SEL_ARGS],
                  load_bars=make_bars, run_suite=CompareGrid(), configure=False)  # fmt: skip
    assert rc == cli.EXIT_OK and art.verify_run(out).ok
    m = manifest(out)
    m["artifacts"] = [a for a in m["artifacts"] if a["path"] != research.COMPARISONS]
    (out / research.COMPARISONS).unlink()
    rewrite_manifest(out, m)
    assert any("required artifacts not listed" in p for p in problems(out))


class CompareGrid(GridSuite):
    """GridSuite plus one comparison row per (slice, strategy, overlay, metric)."""

    def __call__(self, bars: Any, **kw: Any) -> Any:
        report = super().__call__(bars, **kw)
        scen = [s.name for s in kw["scenarios"]]
        for sl in kw["slices"]:
            for sid in [s.strategy_id for s in kw["strategy_engine"].strategies]:
                for sc in scen[1:]:
                    for k in ("sharpe", "annualized_volatility", "max_drawdown", "cagr"):
                        row = {"slice": sl.name, "strategy": sid, "scenario": sc}
                        report.comparisons.append({**row, "vs": scen[0], "metric": k})
        return report


def test_the_three_historical_runs_still_verify_without_changes() -> None:
    root = Path(__file__).resolve().parents[3] / "data" / "risk"
    runs = [
        root / "risk-20261013T093238624963Z-8c93ac17",
        root / "risk-20261013T093533763878Z-234f9ed2",
        root / "risk-20261013T093544891820Z-74b6d609",
    ]
    if not all(r.is_dir() for r in runs):
        pytest.skip("historical runs not present in this checkout")
    for r in runs:
        before = snapshot(r)
        rep = art.verify_run(r)
        assert rep.ok and rep.schema == "risk-research-run/1", rep.problems
        assert snapshot(r) == before  # read-only


# ── L2: metadata coherence ──


def _block(m: dict[str, Any]) -> dict[str, Any]:
    b: dict[str, Any] = m["results"]["daily_returns"]
    return b


Edit = Callable[[dict[str, Any]], object]
METADATA: dict[str, Edit] = {
    "series_cost_scenario": lambda m: _block(m)["series"][0].__setitem__("cost_scenario", "A_zero"),
    "non_finite_count": lambda m: _block(m)["series"][0].__setitem__("non_finite_returns", 7),
    "risk_fingerprint": lambda m: _block(m)["series"][0].__setitem__("risk_fingerprint", "zzz"),
    "backtest_fingerprint": lambda m: _block(m)["series"][0].__setitem__(
        "backtest_fingerprint", "z"
    ),
    "block_initial_capital": lambda m: _block(m).__setitem__("initial_capital", 1.0),
    "block_cost_scenario": lambda m: _block(m).__setitem__("cost_scenario", "A_zero"),
    "duplicated_series": lambda m: _block(m)["series"].append(dict(_block(m)["series"][0])),
    "columns": lambda m: _block(m).__setitem__("columns", ["x"]),
    "convention": lambda m: _block(m).__setitem__("convention", "other"),
    "first_ts": lambda m: _block(m)["series"][0].__setitem__(
        "first_ts", "2000-01-01T00:00:00+00:00"
    ),
    "window_sessions": lambda m: m["results"]["windows"]["full"].__setitem__("sessions", 99),
    "config_initial_capital": lambda m: m["config"]["backtest"]["config"].__setitem__(
        "initial_capital", 5.0
    ),
    "selection_cost": lambda m: m["selection"].__setitem__("cost_scenario", "A_zero"),
    "config_scenario_fingerprint": lambda m: m["config"]["risk_scenarios"][
        "atr_stop_3"
    ].__setitem__("fingerprint", "zzz"),
}


@pytest.mark.parametrize("field", list(METADATA))
def test_metadata_is_cross_checked(run: Path, field: str) -> None:
    m = manifest(run)
    METADATA[field](m)
    rewrite_manifest(run, m)
    assert problems(run), field


def test_series_rows_changed_consistently_are_caught_by_selection(run: Path) -> None:
    """Removing a series from the CSV and the manifest list still conflicts with the selection
    (a whole selection dimension would have to be rewritten too: see the guarantee)."""
    p = run / art.RETURNS_FILE
    lines = p.read_text(encoding="utf-8").splitlines()
    drop = ("early", "sma_trend", "atr_stop_3")
    kept = [lines[0]] + [ln for ln in lines[1:] if tuple(ln.split(",")[:3]) != drop]
    p.write_text("\n".join(kept) + "\n", encoding="utf-8", newline="\n")
    m = manifest(run)
    _block(m)["series"] = [s for s in _block(m)["series"]
                           if (s["slice"], s["strategy"], s["scenario"]) != drop]  # fmt: skip
    _block(m)["rows"] = len(kept) - 1
    rehash(run, m)
    rewrite_manifest(run, m)
    assert any("differ from the selection" in x for x in problems(run))


# ── L3: canonical timestamps, row by row ──


@pytest.mark.parametrize(
    "ts",
    ["2024-01-03T14:30:00", "2024-01-03T14:30:00Z", "2024-01-03T09:30:00-05:00",
     "2024-01-03 14:30:00+00:00", "2024-01-03T14:30+00:00", "2024-01-03T14:30:00.000000+00:00"],
)  # fmt: skip
def test_non_canonical_timestamps_are_refused_in_any_row(run: Path, ts: str) -> None:
    with pytest.raises(ValueError):
        art.parse_ts(ts)
    edit_returns_line(run, 3, 5, ts)  # a middle row, not the first
    assert any("timestamp" in x or "line" in x for x in problems(run))


def test_canonical_timestamps_parse_to_utc() -> None:
    t = art.parse_ts("2024-01-03T14:30:00+00:00")
    assert str(t.tz) == "UTC" and t.isoformat() == "2024-01-03T14:30:00+00:00"
    assert art.parse_ts("2024-01-03T14:30:00.500000+00:00").microsecond == 500000


def test_out_of_order_rows_are_refused(run: Path) -> None:
    lines = (run / art.RETURNS_FILE).read_text(encoding="utf-8").splitlines()
    second_ts = lines[2].split(",")[5]
    edit_returns_line(run, 3, 5, second_ts)  # duplicates the previous session's instant
    assert problems(run)


# ── L4: atomic publication and failures ──


def _names(d: Path) -> list[str]:
    return sorted(x.name for x in d.iterdir())


@pytest.mark.parametrize("stage", ["write", "fsync", "link"])
def test_failure_before_publication_removes_the_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise OSError(f"{stage} failed")

    if stage == "write":
        real_open = open

        def failing_open(path: Any, *a: Any, **k: Any) -> FailingWriter:
            return FailingWriter(real_open(path, *a, **k))

        monkeypatch.setattr(art, "open", failing_open, raising=False)
    else:
        monkeypatch.setattr(os, "fsync" if stage == "fsync" else "link", boom)
    with pytest.raises(OSError, match=f"{stage} failed"):
        art.write_new_atomic(tmp_path / "f.csv", "data")
    assert _names(tmp_path) == []


class FailingWriter:
    """A real (exclusively created) file whose write fails: an I/O error during write."""

    def __init__(self, fh: Any) -> None:
        self.fh = fh

    def __enter__(self) -> "FailingWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.fh.close()

    def write(self, text: str) -> int:
        self.fh.write(text[:2])  # a partial file exists when the error happens
        raise OSError("write failed")


def test_existing_final_file_is_never_replaced(tmp_path: Path) -> None:
    final = tmp_path / "f.csv"
    final.write_text("original", encoding="utf-8")
    with pytest.raises(FileExistsError):
        art.write_new_atomic(final, "new")
    assert final.read_text(encoding="utf-8") == "original" and _names(tmp_path) == ["f.csv"]


def test_a_foreign_temporary_file_is_never_touched(tmp_path: Path) -> None:
    stale = tmp_path / "f.csv.tmp"
    stale.write_text("not ours", encoding="utf-8")
    with pytest.raises(FileExistsError):
        art.write_new_atomic(tmp_path / "f.csv", "new")
    assert stale.read_text(encoding="utf-8") == "not ours" and _names(tmp_path) == ["f.csv.tmp"]


def test_failed_cleanup_is_reported_without_hiding_the_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_link(*a: Any) -> None:
        raise OSError("link failed")

    def no_remove(*a: Any) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(os, "link", no_link)
    monkeypatch.setattr(os, "remove", no_remove)
    with pytest.raises(OSError, match="link failed") as exc:
        art.write_new_atomic(tmp_path / "f.csv", "data")
    assert any("temporary file left behind: f.csv.tmp" in n for n in exc.value.__notes__)
    monkeypatch.undo()
    assert _names(tmp_path) == ["f.csv.tmp"]  # the residue is real and reported, not hidden


def test_cleanup_failure_after_publication_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_remove = os.remove

    def no_remove(path: Any) -> None:
        if str(path).endswith(".tmp"):
            raise PermissionError("locked")
        real_remove(path)

    monkeypatch.setattr(os, "remove", no_remove)
    rc, out = run_grid(tmp_path)
    monkeypatch.undo()
    assert rc == cli.EXIT_FAILED
    m = manifest(out)
    assert m["status"] == research.FAILED and "was published" in m["error"]
    assert not art.verify_run(out).ok


# ── L5: returns matrix ──


@pytest.mark.parametrize(
    "scenarios", [[], ["control_no_overlay", "control_no_overlay"], [1], "control_no_overlay"]
)
def test_matrix_refuses_empty_duplicated_or_invalid_scenarios(run: Path, scenarios: Any) -> None:
    if isinstance(scenarios, str):  # a bare string is one identifier, never its characters
        mat = art.load_returns_matrix(run, slice_name="full", strategy="sma_trend",
                                      scenarios=scenarios)  # fmt: skip
        assert list(mat.columns) == ["control_no_overlay"]
        return
    with pytest.raises(art.ReturnsMatrixError):
        art.load_returns_matrix(run, slice_name="full", strategy="sma_trend", scenarios=scenarios)


def test_matrix_parses_the_same_bytes_it_verified(
    run: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads: list[str] = []
    parsed: list[bytes] = []
    real_read, real_parse = Path.read_bytes, art.parse_returns

    def counting_read(self: Path) -> bytes:
        reads.append(self.name)
        return real_read(self)

    def capturing_parse(data: bytes, name: str = art.RETURNS_FILE) -> Any:
        parsed.append(data)
        return real_parse(data, name)

    monkeypatch.setattr(Path, "read_bytes", counting_read)
    monkeypatch.setattr(art, "parse_returns", capturing_parse)
    art.load_returns_matrix(run, slice_name="full", strategy="sma_trend",
                            scenarios=["control_no_overlay", "atr_stop_3"])  # fmt: skip
    assert reads.count(art.RETURNS_FILE) == 1  # read once: no verify-then-reread
    (entry,) = [a for a in manifest(run)["artifacts"] if a["path"] == art.RETURNS_FILE]
    assert [hashlib.sha256(d).hexdigest() for d in parsed] == [entry["sha256"]]


def test_matrix_values_are_exact(run: Path) -> None:
    before = snapshot(run)
    mat = art.load_returns_matrix(run, slice_name="full", strategy="sma_trend",
                                  scenarios=["control_no_overlay", "atr_stop_3"])  # fmt: skip
    df = art.read_returns(run / art.RETURNS_FILE)
    part = df[(df["slice"] == "full") & (df["strategy"] == "sma_trend")
              & (df["scenario"] == "atr_stop_3")]  # fmt: skip
    assert list(mat["atr_stop_3"]) == list(part["daily_return"])  # bit-for-bit
    assert snapshot(run) == before  # read-only


class ShortSeriesGrid(GridSuite):
    """One series of the `full` slice misses its first session: not aligned with the rest."""

    def __call__(self, bars: Any, **kw: Any) -> Any:
        report = super().__call__(bars, **kw)
        key = ("full", "sma_trend", "atr_stop_3")
        report.runs[key].equity = report.runs[key].equity.iloc[1:]
        return report


def test_unaligned_series_never_reach_the_matrix(tmp_path: Path) -> None:
    rc, out = run_grid(tmp_path, suite=ShortSeriesGrid())
    assert rc == cli.EXIT_OK  # the run records what the suite produced
    assert any("results.windows" in p for p in problems(out))
    with pytest.raises(art.ReturnsMatrixError):
        art.load_returns_matrix(out, slice_name="full", strategy="sma_trend",
                                scenarios=["control_no_overlay", "atr_stop_3"])  # fmt: skip
