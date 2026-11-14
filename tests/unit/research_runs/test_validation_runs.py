"""Phase 5D: the existing Validator recorded in the research run registry.

Synthetic bars (seeded random walk, not market data), a small strategy engine and temporary
directories only. The local database is blocked for every test in this module, and no test
reads the real SPY history or writes under data/."""

import hashlib
import io
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.database import session as db_session
from app.operations.lock import PipelineLock
from app.research_runs import registry as rr
from app.research_runs import validation as rv
from app.research_runs.registry import (
    NOT_APPLICABLE,
    UNKNOWN,
    CodeVersion,
    Fingerprint,
    RunRegistry,
    RunStatus,
)
from app.validation import cli
from app.validation.config import VALIDATION_VERSION, SliceSpec, ValidationConfig
from app.validation.engine import Validator
from app.validation.models import ValidationReport
from tests.unit.strategies.helpers import session_walk, small_engine

REPO = Path(__file__).resolve().parents[3]
N = 260
CODE = CodeVersion("0.15.0", Fingerprint.known("144c593824917fa58ccb6bc60584d1b193c048a2"), True)
SPLIT = session_walk(N, 1).index[150].date()
CFG = ValidationConfig(
    n_resamples=100,
    block_length=10,
    slices=(SliceSpec("full"), SliceSpec("early", end=SPLIT), SliceSpec("late", start=SPLIT)),
)
KEY = Fingerprint.known("synthetic-walk-260")


@pytest.fixture(autouse=True)
def _no_database(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Guarantee: nothing in this module can reach the development database or data/."""

    def blocked(*a: Any, **k: Any) -> Any:
        raise AssertionError("the database must not be used by these tests")

    monkeypatch.setattr("app.database.session.get_session_factory", blocked)
    monkeypatch.setattr("app.database.session.get_engine", blocked)
    monkeypatch.setattr(cli, "get_session_factory", blocked)
    assert REPO not in tmp_path.resolve().parents  # temporary directories outside the repo


def walk(n: int = N, seed: int = 1) -> pd.DataFrame:
    b = session_walk(n, seed)
    return b.assign(adj_close=b["close"] * np.linspace(0.9, 1.0, n))


def validator(cfg: ValidationConfig = CFG) -> Validator:
    return Validator(cfg, strategy_engine=small_engine())


def registry(tmp_path: Path, **kw: Any) -> RunRegistry:
    return RunRegistry(tmp_path / "registry", code_version=lambda: CODE, **kw)


def record(reg: RunRegistry, bars: pd.DataFrame | None = None, **kw: Any) -> rr.RunRecord:
    kw.setdefault("validator", validator())
    kw.setdefault("dataset_key", KEY)
    return rv.record_validation(walk() if bars is None else bars, registry=reg, **kw)


@pytest.fixture(scope="module")
def reference() -> ValidationReport:
    """The same Validator run directly, without the registry."""
    return validator().run(walk())


class BrokenValidator(Validator):
    def run(self, bars: pd.DataFrame) -> ValidationReport:
        raise FloatingPointError("engine exploded\nwith a second line")


# ── 1/2/4. synthetic run, every table persisted, verifiable hashes ──


def test_complete_run_on_synthetic_data(tmp_path: Path) -> None:
    reg = registry(tmp_path)
    rec = record(reg)
    assert rec.status is RunStatus.COMPLETE and rec.problems == () and rec.kind == "validation"
    assert [a.path for a in rec.manifest.artifacts] == sorted(rv.ARTIFACTS)  # type: ignore[union-attr]
    assert reg.latest_complete("validation") == rec
    assert list((reg.root / "staging").iterdir()) == []  # moved, nothing left behind


def test_every_table_is_persisted_exactly(tmp_path: Path, reference: ValidationReport) -> None:
    reg = registry(tmp_path)
    rec = record(reg)
    base = reg.published / rec.run_id
    expected = reference.frames()
    assert tuple(expected) == rv.FRAMES
    for name, frame in expected.items():
        assert not frame.empty, name
        written = (base / f"{name}.csv").read_bytes()
        assert written == frame.to_csv(index=False, lineterminator="\n").encode()  # deterministic
    comps = pd.read_csv(base / "comparisons.csv")
    assert (comps["family"] == "formal").sum() == 30  # the pre-declared formal family
    assert set(pd.read_csv(base / "cost_sensitivity.csv")["scenario"]) == {
        c.name for c in CFG.cost_scenarios
    }
    summary = json.loads((base / rv.SUMMARY).read_text(encoding="utf-8"))
    assert summary["config_fingerprint"] == CFG.fingerprint() and summary["intervals"] == len(
        expected["intervals"]
    )
    assert "not evidence of future profitability" in summary["note"]


def test_recorded_hashes_are_verifiable(tmp_path: Path) -> None:
    reg = registry(tmp_path)
    rec = record(reg)
    base = reg.published / rec.run_id
    for a in rec.manifest.artifacts:  # type: ignore[union-attr]
        data = (base / a.path).read_bytes()
        assert (a.sha256, a.bytes) == (hashlib.sha256(data).hexdigest(), len(data))
    events = [json.loads(x) for x in reg.index_path.read_text(encoding="utf-8").splitlines()]
    assert [e["event"] for e in events] == ["started", "completed"]
    assert (
        events[1]["manifest_sha256"]
        == hashlib.sha256((base / rr.MANIFEST).read_bytes()).hexdigest()
    )
    assert events[0]["required_artifacts"] == sorted(rv.ARTIFACTS)


# ── 3. manifest metadata ──


def test_manifest_metadata(tmp_path: Path) -> None:
    reg = registry(tmp_path)
    bars = walk()
    v = validator()
    rec = record(reg, bars, validator=v, source={"kind": "synthetic", "seed": 1})
    m = rec.manifest
    assert m is not None
    assert (m.kind, m.format_version, m.status) == ("validation", rr.FORMAT_VERSION, "complete")
    assert m.config_fingerprint == Fingerprint.known(CFG.fingerprint())
    assert m.data_fingerprint == rv.data_fingerprint(bars) and m.dataset_key == KEY
    assert m.code == CODE
    assert m.started_at <= m.finished_at
    p = m.parameters
    assert p["validation_version"] == VALIDATION_VERSION
    assert (
        p["validation_config"]["n_resamples"] == 100 and p["validation_config"]["seed"] == CFG.seed
    )
    assert p["validation_config"]["slices"][1] == {
        "name": "early",
        "start": None,
        "end": SPLIT.isoformat(),
    }
    assert p["backtest_config_fingerprint"] == v.backtest_config.fingerprint()
    assert p["strategy_engine_fingerprint"] == v.strategy_engine.fingerprint()
    assert p["input"]["rows"] == N and p["input"]["first_ts"] == bars.index[0].isoformat()
    assert p["input"]["columns"] == list(rv.INPUT_COLUMNS) and p["source"]["seed"] == 1


def test_unknown_metadata_is_explicit(tmp_path: Path) -> None:
    reg = RunRegistry(
        tmp_path / "registry", code_version=lambda: CodeVersion("0.15.0", UNKNOWN, None)
    )
    rec = rv.record_validation(walk(), registry=reg, validator=validator())
    m = rec.manifest
    assert m is not None and m.dataset_key == UNKNOWN and m.code.git_commit == UNKNOWN
    assert m.code.git_dirty is None and m.parameters["source"] == {"kind": "in-memory bars"}
    raw = json.loads((reg.published / rec.run_id / rr.MANIFEST).read_text(encoding="utf-8"))
    assert raw["dataset_key"] == {"state": "unknown", "value": None}
    na = record(registry(tmp_path / "b"), dataset_key=NOT_APPLICABLE).manifest
    assert na is not None and na.dataset_key == NOT_APPLICABLE


def test_data_fingerprint_tracks_every_input_column_and_the_index() -> None:
    base = walk()
    fp = rv.data_fingerprint(base)
    assert fp == rv.data_fingerprint(walk()) and fp.state == "known"
    for col in rv.INPUT_COLUMNS:
        changed = base.copy()
        changed.loc[changed.index[-1], col] += 1.0
        assert rv.data_fingerprint(changed) != fp, col
    shifted = base.copy()
    shifted.index = shifted.index + pd.Timedelta(days=1)
    assert rv.data_fingerprint(shifted) != fp
    assert rv.data_fingerprint(base.assign(extra=1.0)) == fp  # unused columns do not count


# ── 5. retrieval ──


def test_retrieve_and_read_artifacts(tmp_path: Path, reference: ValidationReport) -> None:
    reg = registry(tmp_path)
    first = record(reg)
    second = record(reg)
    rec, frames, summary = rv.load_validation_run(RunRegistry(reg.root))  # a fresh reader
    assert rec.run_id == second.run_id and summary["formal_family_size"] == 30
    expected = reference.frames()
    for name in rv.FRAMES:
        exp = pd.read_csv(io.StringIO(expected[name].to_csv(index=False)))
        pd.testing.assert_frame_equal(frames[name], exp)
    assert rv.load_validation_run(reg, first.run_id)[0].run_id == first.run_id
    with pytest.raises(LookupError):
        rv.load_validation_run(reg, "validation-20261011T120000000000Z-0000abcd")


def test_retrieval_refuses_when_nothing_is_complete(tmp_path: Path) -> None:
    with pytest.raises(LookupError, match="no complete validation run"):
        rv.load_validation_run(registry(tmp_path))
    assert not (tmp_path / "registry").exists()  # reading created nothing


# ── 6/7/9. failures: never complete, earlier runs preserved ──


def test_engine_failure_is_recorded_as_failed(tmp_path: Path) -> None:
    reg = registry(tmp_path)
    good = record(reg)
    with pytest.raises(rv.ValidationRunError) as info:
        record(reg, validator=BrokenValidator(CFG, strategy_engine=small_engine()))
    err = info.value
    assert err.record is not None and err.record.status is RunStatus.FAILED
    assert err.record.failure == "FloatingPointError: engine exploded with a second line"
    assert reg.get(err.run_id).status is RunStatus.FAILED
    assert reg.latest_complete("validation").run_id == good.run_id  # type: ignore[union-attr]
    with pytest.raises(LookupError, match="failed"):
        rv.load_validation_run(reg, err.run_id)


def test_real_engine_error_on_bad_values_is_recorded_as_failed(tmp_path: Path) -> None:
    reg = registry(tmp_path)
    bars = walk()
    bars.loc[bars.index[200], "adj_close"] = np.nan  # structurally valid, unusable
    with pytest.raises(rv.ValidationRunError) as info:
        record(reg, bars)
    assert reg.get(info.value.run_id).status is RunStatus.FAILED
    assert reg.latest_complete("validation") is None


@pytest.mark.parametrize("target", ["comparisons.csv", rv.SUMMARY])
def test_artifact_write_failure_never_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    reg = registry(tmp_path)
    good = record(reg)
    before = {p.name: p.read_bytes() for p in (reg.published / good.run_id).iterdir()}
    real = rr.RunHandle.write_artifact

    def flaky(self: rr.RunHandle, name: str, data: bytes | str) -> None:
        if name == target:
            raise OSError("disk full")
        real(self, name, data)

    monkeypatch.setattr(rr.RunHandle, "write_artifact", flaky)
    with pytest.raises(rv.ValidationRunError) as info:
        record(reg)
    monkeypatch.undo()
    assert reg.get(info.value.run_id).status is RunStatus.FAILED
    assert info.value.record.failure == "OSError: disk full"  # type: ignore[union-attr]
    assert not (reg.published / info.value.run_id).exists()
    assert {p.name: p.read_bytes() for p in (reg.published / good.run_id).iterdir()} == before
    assert reg.latest_complete("validation").run_id == good.run_id  # type: ignore[union-attr]


def test_manifest_write_failure_never_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = registry(tmp_path)

    def broken(path: Path, text: str) -> None:
        raise OSError("cannot write manifest")

    monkeypatch.setattr(rr, "atomic_write", broken)
    with pytest.raises(rv.ValidationRunError) as info:
        record(reg)
    monkeypatch.undo()
    assert reg.get(info.value.run_id).status is RunStatus.FAILED
    assert reg.latest_complete("validation") is None


def test_missing_table_blocks_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = registry(tmp_path)
    real = rr.RunHandle.write_artifact

    def skip(self: rr.RunHandle, name: str, data: bytes | str) -> None:
        if name != "redundancy.csv":  # silently lost table
            real(self, name, data)

    monkeypatch.setattr(rr.RunHandle, "write_artifact", skip)
    with pytest.raises(rv.ValidationRunError, match="required artifacts missing"):
        record(reg)
    monkeypatch.undo()
    rec = reg.records()[0]
    assert rec.status is RunStatus.FAILED and "redundancy.csv" in (rec.failure or "")
    assert reg.latest_complete("validation") is None


def test_registry_refusing_the_failure_leaves_the_run_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = registry(tmp_path)

    def busy(self: rr.RunHandle, reason: str) -> rr.RunRecord:
        raise rr.RegistryBusyError("registry lock busy")

    monkeypatch.setattr(rr.RunHandle, "fail", busy)
    with pytest.raises(rv.ValidationRunError, match="could not record the failure") as info:
        record(reg, validator=BrokenValidator(CFG, strategy_engine=small_engine()))
    monkeypatch.undo()
    assert info.value.record is None
    assert reg.get(info.value.run_id).status is RunStatus.INCOMPLETE  # never complete


# ── 8. missing or modified files ──


@pytest.mark.parametrize(
    ("damage", "problem"),
    [
        (lambda d: (d / "intervals.csv").unlink(), "artifact missing: intervals.csv"),
        (
            lambda d: (d / "comparisons.csv").write_bytes(
                (d / "comparisons.csv").read_bytes().replace(b"formal", b"FORMAL", 1)
            ),
            "artifact altered: comparisons.csv",
        ),
        (
            lambda d: (d / rv.SUMMARY).write_text("{}", encoding="utf-8"),
            "artifact altered: summary.json",
        ),
    ],
)
def test_missing_or_modified_files_are_detected(tmp_path: Path, damage: Any, problem: str) -> None:
    reg = registry(tmp_path)
    good = record(reg)
    bad = record(reg)
    damage(reg.published / bad.run_id)
    rec = reg.get(bad.run_id)
    assert rec.status is RunStatus.INTEGRITY_FAILED and problem in rec.problems
    with pytest.raises(LookupError, match="integrity_failed"):
        rv.load_validation_run(reg, bad.run_id)
    assert rv.load_validation_run(reg)[0].run_id == good.run_id  # falls back to the verified one
    assert cli.main(["verify", "--registry", str(reg.root), "--run-id", bad.run_id]) == cli.EXIT_RUN


# ── 10. invalid parameters and inputs ──


def _bad_inputs() -> list[Any]:
    b = walk()
    naive = b.copy()
    naive.index = pd.DatetimeIndex(naive.index).tz_localize(None)
    dup = pd.concat([b.iloc[:5], b.iloc[4:5]])
    return [
        "not a frame",
        b.iloc[0:0],
        b.drop(columns=["adj_close"]),
        b.reset_index(drop=True),
        naive,
        b.iloc[::-1],
        dup,
    ]


@pytest.mark.parametrize("bars", _bad_inputs())
def test_invalid_bars_are_rejected_before_registering(tmp_path: Path, bars: Any) -> None:
    reg = registry(tmp_path)
    with pytest.raises(ValueError):
        rv.record_validation(bars, registry=reg, validator=validator())
    assert not reg.root.exists()  # no run, no directory


@pytest.mark.parametrize(
    "kw",
    [{"validator": object()}, {"dataset_key": "abc"}, {"source": {"x": object()}}],
)
def test_invalid_arguments_are_rejected_before_registering(
    tmp_path: Path, kw: dict[str, Any]
) -> None:
    reg = registry(tmp_path)
    with pytest.raises((ValueError, TypeError)):
        record(reg, **kw)
    assert not reg.root.exists()


def test_invalid_validation_config_is_rejected() -> None:
    with pytest.raises(ValueError):
        ValidationConfig(n_resamples=10)  # the engine's own checks still apply


# ── 11. CLI: explicit only, no unexpected side effects ──


def _fake_load(symbol: str) -> tuple[pd.DataFrame, Fingerprint, dict[str, Any]]:
    assert symbol == "SPY"
    return walk(), KEY, {"kind": "synthetic (test)", "symbol": symbol}


def test_cli_record_and_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "_load_dataset", _fake_load)
    monkeypatch.setattr(cli, "_make_validator", validator)
    root = tmp_path / "registry"
    assert cli.main(["record", "--symbol", "spy", "--registry", str(root)]) == cli.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "complete" and out["formal_family_size"] == 30
    assert len(out["artifacts"]) == len(rv.ARTIFACTS) and "not evidence" in out["note"]
    rec = RunRegistry(root).get(out["run_id"])
    assert rec.status is RunStatus.COMPLETE and rec.manifest.parameters["source"]["symbol"] == "SPY"  # type: ignore[union-attr]
    assert cli.main(["verify", "--registry", str(root), "--run-id", out["run_id"]]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["status"] == "complete"
    assert cli.main(["verify", "--registry", str(root)]) == cli.EXIT_OK
    listing = json.loads(capsys.readouterr().out)
    assert listing["latest_complete"] == out["run_id"] and listing["unregistered"] == []


def test_cli_engine_failure_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "_load_dataset", _fake_load)
    monkeypatch.setattr(
        cli, "_make_validator", lambda: BrokenValidator(CFG, strategy_engine=small_engine())
    )
    root = tmp_path / "registry"
    assert cli.main(["record", "--symbol", "SPY", "--registry", str(root)]) == cli.EXIT_RUN
    assert "failed: FloatingPointError" in capsys.readouterr().err
    assert cli.main(["verify", "--registry", str(root)]) == cli.EXIT_RUN  # nothing complete


def test_cli_busy_registry_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "_load_dataset", _fake_load)
    monkeypatch.setattr(cli, "_make_validator", validator)
    root = tmp_path / "registry"
    assert cli.main(["record", "--symbol", "SPY", "--registry", str(root)]) == cli.EXIT_OK
    capsys.readouterr()
    before = root.joinpath("index.jsonl").read_bytes()
    monkeypatch.setattr(rr, "RunRegistry", lambda p: RunRegistry(p, lock_timeout=0.2))
    holder = PipelineLock(root, purpose="test-holder")
    holder.acquire()
    try:
        assert cli.main(["record", "--symbol", "SPY", "--registry", str(root)]) == cli.EXIT_REGISTRY
    finally:
        holder.release()
    assert "registry busy (nothing published)" in capsys.readouterr().err
    assert root.joinpath("index.jsonl").read_bytes() == before  # history untouched
    assert RunRegistry(root).latest_complete("validation") is not None


def test_cli_corrupt_registry_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "_load_dataset", _fake_load)
    monkeypatch.setattr(cli, "_make_validator", validator)
    root = tmp_path / "registry"
    root.mkdir()
    (root / "index.jsonl").write_text("not json\n", encoding="utf-8")
    assert cli.main(["record", "--symbol", "SPY", "--registry", str(root)]) == cli.EXIT_REGISTRY
    assert cli.main(["verify", "--registry", str(root)]) == cli.EXIT_REGISTRY
    assert "corrupt" in capsys.readouterr().err
    assert (root / "index.jsonl").read_text(encoding="utf-8") == "not json\n"  # preserved


@pytest.mark.parametrize(
    ("argv", "code"),
    [
        (["record", "--symbol", "S P Y", "--registry", "{reg}"], cli.EXIT_INPUT),
        (["record", "--symbol", "SPY", "--registry", "{file}"], cli.EXIT_INPUT),
        (["verify", "--registry", "{file}"], cli.EXIT_INPUT),
        (["verify", "--registry", "{reg}", "--run-id", "nope"], cli.EXIT_INPUT),
    ],
)
def test_cli_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], code: int
) -> None:
    def never(symbol: str) -> Any:
        raise AssertionError("no data may be loaded for invalid input")

    monkeypatch.setattr(cli, "_load_dataset", never)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    args = [a.format(reg=tmp_path / "reg", file=tmp_path / "file") for a in argv]
    assert cli.main(args) == code
    assert not (tmp_path / "reg").exists()


@pytest.mark.parametrize("argv", [["record", "--symbol", "SPY"], ["record"], ["verify"], []])
def test_cli_requires_explicit_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as info:
        cli.main(argv)
    assert info.value.code == 2 and list(tmp_path.iterdir()) == []  # no implicit registry


def test_cli_data_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    from app.dashboard.services.market import NoDataError

    def no_data(symbol: str) -> Any:
        raise NoDataError("instrument 'QQQ' not found in the local database")

    def unreachable(symbol: str) -> Any:
        raise ConnectionError("postgresql://user:SECRET@host/db unreachable")

    root = tmp_path / "registry"
    monkeypatch.setattr(cli, "_load_dataset", no_data)
    assert cli.main(["record", "--symbol", "QQQ", "--registry", str(root)]) == cli.EXIT_DATA
    monkeypatch.setattr(cli, "_load_dataset", unreachable)
    assert cli.main(["record", "--symbol", "SPY", "--registry", str(root)]) == cli.EXIT_DATA
    err = capsys.readouterr().err
    assert "not found" in err and "ConnectionError" in err and "SECRET" not in err
    assert not root.exists()


def test_load_dataset_reads_in_a_read_only_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real loader, against a fake session: READ ONLY first, then rolled back."""
    from app.dashboard.services import market

    statements: list[str] = []

    class FakeSession:
        def __enter__(self) -> "FakeSession":
            return self

        def __exit__(self, *exc: object) -> None:
            statements.append("close")

        def execute(self, stmt: Any) -> None:
            statements.append(str(stmt))

        def rollback(self) -> None:
            statements.append("rollback")

    ident = market.DatasetIdentity(
        "SPY", 1, "yfinance", "1d", N, pd.Timestamp("2015-01-02", tz="UTC"),
        pd.Timestamp("2016-01-04", tz="UTC"), pd.Timestamp("2026-10-10", tz="UTC"),
    )  # fmt: skip
    monkeypatch.setattr(cli, "get_session_factory", lambda: FakeSession)
    monkeypatch.setattr(
        cli, "get_settings", lambda: SimpleNamespace(log_level="INFO", log_format="json")
    )
    monkeypatch.setattr(cli, "configure_logging", lambda *a: None)
    monkeypatch.setattr(market, "dataset_identity", lambda s, symbol: ident)
    monkeypatch.setattr(market, "load_bars", lambda s, i: walk())
    bars, key, source = cli._load_dataset("SPY")
    assert statements == ["SET TRANSACTION READ ONLY", "rollback", "close"]
    assert key == Fingerprint.known(ident.key) and source["bars"] == N and len(bars) == N
    assert source["last_ingested_at"] == "2026-10-10T00:00:00+00:00"


PRINT_MODULES = "import json, sys; print(json.dumps(sorted(sys.modules)))"


def _modules_after(code: str) -> list[str]:
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-B", "-c", f"{code}; {PRINT_MODULES}"],
        capture_output=True, text=True, check=True, cwd=REPO,
    )  # fmt: skip
    return list(json.loads(out.stdout.strip().splitlines()[-1]))


def test_importing_the_cli_runs_nothing(tmp_path: Path) -> None:
    mods = _modules_after("import app.validation.cli")
    assert not any(m.startswith(("app.research_runs", "app.dashboard", "app.ml")) for m in mods)
    assert "sklearn" not in mods


def test_api_does_not_load_the_validation_runner() -> None:
    mods = _modules_after("import app.api.main")
    assert not any(m.startswith(("app.research_runs", "app.validation.engine")) for m in mods)


# ── 12. isolation from real data ──


def test_tests_use_synthetic_bars_and_temporary_directories(tmp_path: Path) -> None:
    bars = walk()
    assert bars.index[0].date() == date(2015, 1, 2) and len(bars) == N  # synthetic walk, not SPY
    reg = registry(tmp_path)
    assert (
        REPO not in reg.root.resolve().parents and (REPO / "data") not in reg.root.resolve().parents
    )
    with pytest.raises(AssertionError, match="database must not be used"):
        db_session.get_session_factory()


# ── R5: CLI error contract (exit code + message per error type) ──


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (ValueError("bars are empty"), cli.EXIT_INPUT, "invalid input: bars are empty"),
        (TypeError("cannot record parameter"), cli.EXIT_INPUT, "invalid input: cannot record"),
        (
            rr.ManifestError("invalid artifact name"),
            cli.EXIT_INPUT,
            "invalid input: invalid artifact",
        ),
        (rr.RegistryBusyError("lock busy"), cli.EXIT_REGISTRY, "registry busy (nothing published)"),
        (
            rr.RegistryCorruptError("line 1: hash chain broken"),
            cli.EXIT_REGISTRY,
            "registry history is corrupt (nothing published)",
        ),
        (
            rr.RegistryError("unknown run kind"),
            cli.EXIT_REGISTRY,
            "registry error (nothing published)",
        ),
        (
            PermissionError(13, "denied"),
            cli.EXIT_REGISTRY,
            "registry unavailable (nothing published)",
        ),
        (
            rv.ValidationRunError("validation-x", None, "RegistryBusyError: busy"),
            cli.EXIT_RUN,
            "validation run validation-x incomplete",
        ),
    ],
)
def test_cli_error_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
    error: Exception,
    code: int,
    message: str,
) -> None:
    def raising(*a: Any, **k: Any) -> Any:
        raise error

    monkeypatch.setattr(cli, "_load_dataset", _fake_load)
    monkeypatch.setattr(rv, "record_validation", raising)
    assert cli.main(["record", "--symbol", "SPY", "--registry", str(tmp_path / "r")]) == code
    assert message in capsys.readouterr().err


def test_cli_refused_overwrite_is_a_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The registry refuses to overwrite a published run: the run is recorded as failed (1)."""
    fixed = "validation-20261011T120000000000Z-0000abcd"
    monkeypatch.setattr(cli, "_load_dataset", _fake_load)
    monkeypatch.setattr(cli, "_make_validator", validator)
    monkeypatch.setattr(RunRegistry, "new_run_id", lambda self, kind: fixed)
    root = tmp_path / "registry"
    (root / "runs" / fixed).mkdir(parents=True)  # something already published under that id
    (root / "runs" / fixed / "keep.txt").write_text("untouched", encoding="utf-8")
    assert cli.main(["record", "--symbol", "SPY", "--registry", str(root)]) == cli.EXIT_RUN
    err = capsys.readouterr().err
    assert f"validation run {fixed} failed" in err and "refusing to overwrite" in err
    assert (root / "runs" / fixed / "keep.txt").read_text(encoding="utf-8") == "untouched"
    assert RunRegistry(root).get(fixed).status is RunStatus.FAILED


def test_cli_unusable_registry_location_is_a_registry_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr(cli, "_load_dataset", _fake_load)
    monkeypatch.setattr(cli, "_make_validator", validator)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    target = tmp_path / "file" / "registry"  # parent is a file: cannot be created
    assert cli.main(["record", "--symbol", "SPY", "--registry", str(target)]) == cli.EXIT_REGISTRY
    assert "registry unavailable (nothing published)" in capsys.readouterr().err


# ── Phase 6, B6 through the validation integration ──


def test_record_failure_after_publication_is_failed_and_files_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = registry(tmp_path)
    original = reg._append

    def crash(body: dict[str, Any]) -> None:
        if body["event"] == "completed":
            raise OSError("cannot append the completed event (injected)")
        original(body)

    monkeypatch.setattr(reg, "_append", crash)
    with pytest.raises(rv.ValidationRunError) as info:
        record(reg)
    monkeypatch.undo()
    err = info.value
    assert err.record is not None and err.record.status is RunStatus.FAILED
    assert any(
        "published directory present although the run failed" in p for p in err.record.problems
    )
    assert sorted(p.name for p in (reg.published / err.run_id).iterdir()) == sorted(
        (*rv.ARTIFACTS, rr.MANIFEST)
    )
    with pytest.raises(LookupError, match="failed"):
        rv.load_validation_run(reg, err.run_id)
    assert cli.main(["verify", "--registry", str(reg.root), "--run-id", err.run_id]) == cli.EXIT_RUN
