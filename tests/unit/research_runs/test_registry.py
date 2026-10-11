"""Research run registry: states, staged publication, SHA-256 verification, append-only history,
concurrency and transitions. Every test uses its own temporary directory (never data/)."""

import copy
import hashlib
import json
import re
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.core.integrity import entry_hash
from app.operations.lock import PipelineLock
from app.research_runs import registry as rr
from app.research_runs.registry import (
    NOT_APPLICABLE,
    UNKNOWN,
    CodeVersion,
    Fingerprint,
    InvalidTransitionError,
    ManifestError,
    RegistryBusyError,
    RegistryCorruptError,
    RegistryError,
    RunManifest,
    RunRegistry,
    RunStatus,
)

CODE = CodeVersion("0.15.0", Fingerprint.known("144c593824917fa58ccb6bc60584d1b193c048a2"), True)
CFG = Fingerprint.known("cfg-1234abcd")
DATA = Fingerprint.known("data-5678ef90")


def make(tmp_path: Path, **kw: Any) -> RunRegistry:
    return RunRegistry(tmp_path / "registry", code_version=lambda: CODE, **kw)


def start(
    reg: RunRegistry, kind: str = "validation", required: tuple[str, ...] = ("a.csv",)
) -> rr.RunHandle:
    return reg.start(
        kind,  # type: ignore[arg-type]
        config_fingerprint=CFG,
        data_fingerprint=DATA,
        dataset_key=UNKNOWN,
        parameters={"seed": 20261007, "slices": ["full", "early", "late"]},
        required_artifacts=required,
    )


def publish(reg: RunRegistry, files: dict[str, str] | None = None, **kw: Any) -> rr.RunRecord:
    files = files or {"a.csv": "x,y\n1,2\n", "b.json": '{"k": 1}'}
    h = start(reg, **kw)
    for name, content in files.items():
        h.write_artifact(name, content)
    return h.publish()


# ── 1. create and retrieve a valid run ──


def test_create_and_retrieve_a_valid_run(tmp_path: Path) -> None:
    reg = make(tmp_path)
    rec = publish(reg)
    assert rec.status is RunStatus.COMPLETE and rec.problems == ()
    m = rec.manifest
    assert m is not None and m.format_version == rr.FORMAT_VERSION and m.status == "complete"
    assert (m.kind, m.config_fingerprint, m.data_fingerprint, m.dataset_key) == (
        "validation",
        CFG,
        DATA,
        UNKNOWN,
    )
    assert m.code == CODE and m.parameters["seed"] == 20261007
    assert [a.path for a in m.artifacts] == ["a.csv", "b.json"]
    fresh = RunRegistry(tmp_path / "registry")  # a new reader sees the same verified run
    again = fresh.get(rec.run_id)
    assert again.status is RunStatus.COMPLETE and again.manifest == m
    assert fresh.latest_complete("validation") == again
    assert fresh.latest_complete("ml") is None
    assert (
        fresh.latest_complete("validation", config_fingerprint=Fingerprint.known("other")) is None
    )
    assert (
        fresh.latest_complete("validation", config_fingerprint=CFG, data_fingerprint=DATA) == again
    )


def test_history_is_preserved_and_latest_is_the_newest_complete(tmp_path: Path) -> None:
    reg = make(tmp_path)
    first = publish(reg)
    second = publish(reg)
    assert [r.run_id for r in reg.records()] == [first.run_id, second.run_id]
    assert reg.latest_complete("validation").run_id == second.run_id  # type: ignore[union-attr]
    assert reg.get(first.run_id).status is RunStatus.COMPLETE  # never replaced


# ── 2. unique identifiers ──


def test_run_ids_are_unique_and_well_formed(tmp_path: Path) -> None:
    fixed = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    reg = make(tmp_path, clock=lambda: fixed)  # same timestamp: uniqueness from the random part
    ids = [reg.new_run_id("ml") for _ in range(500)]
    assert len(set(ids)) == 500
    assert all(re.fullmatch(r"ml-20261011T120000000000Z-[0-9a-f]{8}", i) for i in ids)
    with pytest.raises(RegistryError):
        reg.new_run_id("trading")  # type: ignore[arg-type]


# ── 3. metadata and format version ──


@pytest.mark.parametrize(
    ("state", "value"),
    [("known", None), ("known", ""), ("known", "has space"), ("unknown", "x"), ("maybe", None)],
)
def test_invalid_fingerprints_are_rejected(state: str, value: str | None) -> None:
    with pytest.raises(ManifestError):
        Fingerprint(state, value)  # type: ignore[arg-type]


def test_explicit_unknown_and_not_applicable_round_trip() -> None:
    for fp in (UNKNOWN, NOT_APPLICABLE, Fingerprint.known("abc")):
        assert Fingerprint.from_dict(fp.to_dict()) == fp
    assert UNKNOWN.to_dict() == {"state": "unknown", "value": None}


def _valid_manifest(tmp_path: Path) -> dict[str, Any]:
    reg = make(tmp_path)
    rec = publish(reg)
    d: dict[str, Any] = json.loads((reg.published / rec.run_id / rr.MANIFEST).read_text("utf-8"))
    return d


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("artifacts"),
        lambda d: d.update(format_version=2),
        lambda d: d.update(status="running"),
        lambda d: d.update(kind="ml"),
        lambda d: d.update(run_id="not-an-id"),
        lambda d: d.update(started_at="yesterday"),
        lambda d: d.update(extra="x"),
        lambda d: d["artifacts"].reverse(),
        lambda d: d["artifacts"][0].update(sha256="0" * 63),
        lambda d: d["artifacts"][0].update(path="../escape.csv"),
        lambda d: d["artifacts"][0].update(bytes=-1),
        lambda d: d["code"].update(git_dirty="yes"),
        lambda d: d.update(artifacts=[]),
    ],
)
def test_manifest_validation_rejects_invalid_metadata(tmp_path: Path, mutate: Any) -> None:
    d = _valid_manifest(tmp_path)
    RunManifest.from_dict(copy.deepcopy(d))  # the untouched manifest is valid
    mutate(d)
    with pytest.raises(ManifestError):
        RunManifest.from_dict(d)


def test_start_validates_inputs(tmp_path: Path) -> None:
    reg = make(tmp_path)
    kw: dict[str, Any] = dict(
        config_fingerprint=CFG,
        data_fingerprint=DATA,
        dataset_key=UNKNOWN,
        parameters={},
        required_artifacts=("a.csv",),
    )
    with pytest.raises(RegistryError):
        reg.start("trading", **kw)  # type: ignore[arg-type]
    for bad in ({"x": float("nan")}, {"x": object()}):
        with pytest.raises(ManifestError):
            reg.start("ml", **{**kw, "parameters": bad})
    for names in ((), ("run.json",), ("../x",), ("a b",)):
        with pytest.raises(ManifestError):
            reg.start("ml", **{**kw, "required_artifacts": names})
    with pytest.raises(ManifestError):
        reg.start("ml", **{**kw, "config_fingerprint": "abc"})
    assert reg.records() == []  # nothing was registered by the rejected calls


def test_artifact_names_are_validated(tmp_path: Path) -> None:
    h = start(make(tmp_path))
    for name in ("run.json", "../x.csv", "sub/x.csv", "x.tmp", ".hidden", ""):
        with pytest.raises(ManifestError):
            h.write_artifact(name, "x")


# ── 4/5. hashes: correct, missing, altered, extra ──


def test_recorded_hashes_match_the_files(tmp_path: Path) -> None:
    reg = make(tmp_path)
    rec = publish(reg, {"a.csv": "1,2\n", "blob.bin": "\x00binary"})
    d = reg.published / rec.run_id
    for a in rec.manifest.artifacts:  # type: ignore[union-attr]
        data = (d / a.path).read_bytes()
        assert a.sha256 == hashlib.sha256(data).hexdigest() and a.bytes == len(data)
    index = [json.loads(x) for x in reg.index_path.read_text(encoding="utf-8").splitlines()]
    completed = next(e for e in index if e["event"] == "completed")
    assert (
        completed["manifest_sha256"] == hashlib.sha256((d / rr.MANIFEST).read_bytes()).hexdigest()
    )


def test_bytes_artifacts_are_written_exactly(tmp_path: Path) -> None:
    reg = make(tmp_path)
    h = start(reg)
    payload = bytes(range(256)) * 10
    h.write_artifact("a.csv", payload)
    rec = h.publish()
    assert (reg.published / rec.run_id / "a.csv").read_bytes() == payload


@pytest.mark.parametrize(
    ("damage", "expected"),
    [
        (lambda d: (d / "a.csv").unlink(), "artifact missing: a.csv"),
        (
            lambda d: (d / "a.csv").write_text("tampered", encoding="utf-8"),
            "artifact altered: a.csv",
        ),
        (lambda d: (d / "c.csv").write_text("new", encoding="utf-8"), "unexpected file: c.csv"),
        (lambda d: (d / rr.MANIFEST).unlink(), "published manifest missing"),
        (
            lambda d: (d / rr.MANIFEST).write_text(
                (d / rr.MANIFEST)
                .read_text(encoding="utf-8")
                .replace('"seed": 20261007', '"seed": 1'),
                encoding="utf-8",
            ),
            "manifest does not match the hash recorded at completion",
        ),
        (lambda d: (d / rr.MANIFEST).write_text("{not json", encoding="utf-8"), "invalid manifest"),
    ],
)
def test_integrity_failures_are_detected_and_never_complete(
    tmp_path: Path, damage: Any, expected: str
) -> None:
    reg = make(tmp_path)
    good = publish(reg)
    bad = publish(reg)
    damage(reg.published / bad.run_id)
    rec = reg.get(bad.run_id)
    assert rec.status is RunStatus.INTEGRITY_FAILED
    assert any(p.startswith(expected) for p in rec.problems), rec.problems
    assert reg.latest_complete("validation").run_id == good.run_id  # type: ignore[union-attr]
    assert reg.get(good.run_id).status is RunStatus.COMPLETE


# ── 6/7. interruption: incomplete is never complete ──


def test_interrupted_before_publication_is_incomplete(tmp_path: Path) -> None:
    reg = make(tmp_path)
    h = start(reg)
    h.write_artifact("a.csv", "partial")
    rec = reg.get(h.run_id)
    assert rec.status is RunStatus.INCOMPLETE and rec.manifest is None
    assert reg.latest_complete("validation") is None
    assert (reg.staging / h.run_id / "a.csv").exists()  # kept, never deleted
    assert not (reg.published / h.run_id).exists()


def test_manifest_alone_is_not_enough(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Crash after the directory was published but before the completed event."""
    reg = make(tmp_path)
    h = start(reg)
    h.write_artifact("a.csv", "x")
    original = reg._append

    def crash(body: dict[str, Any]) -> None:
        if body["event"] == "completed":
            raise OSError("power loss")
        original(body)

    monkeypatch.setattr(reg, "_append", crash)
    with pytest.raises(OSError):
        h.publish()
    monkeypatch.undo()
    assert (reg.published / h.run_id / rr.MANIFEST).exists()  # a complete-looking manifest...
    rec = reg.get(h.run_id)
    assert rec.status is RunStatus.INCOMPLETE  # ...is still not a complete run
    assert "published directory present without a completed event" in rec.problems
    assert reg.latest_complete("validation") is None


def test_missing_required_artifact_blocks_publication(tmp_path: Path) -> None:
    reg = make(tmp_path)
    h = start(reg, required=("a.csv", "b.csv"))
    h.write_artifact("a.csv", "x")
    with pytest.raises(RegistryError, match="required artifacts missing"):
        h.publish()
    assert reg.get(h.run_id).status is RunStatus.INCOMPLETE


def test_leftover_temporary_file_blocks_publication(tmp_path: Path) -> None:
    reg = make(tmp_path)
    h = start(reg)
    h.write_artifact("a.csv", "x")
    (reg.staging / h.run_id / "b.csv.tmp").write_text("half", encoding="utf-8")
    with pytest.raises(RegistryError, match="unexpected files"):
        h.publish()
    assert reg.get(h.run_id).status is RunStatus.INCOMPLETE


# ── 8. failure while writing never harms earlier runs ──


def test_failure_during_write_keeps_previous_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reg = make(tmp_path)
    good = publish(reg)
    before = {p.name: p.read_bytes() for p in (reg.published / good.run_id).iterdir()}
    h = start(reg)
    h.write_artifact("a.csv", "x")

    def broken(path: Path, text: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rr, "atomic_write", broken)  # the manifest write fails
    with pytest.raises(OSError):
        h.publish()
    monkeypatch.undo()
    assert reg.get(h.run_id).status is RunStatus.INCOMPLETE
    failed = h.fail("disk full while writing the manifest")
    assert failed.status is RunStatus.FAILED and failed.failure.startswith("disk full")  # type: ignore[union-attr]
    assert {p.name: p.read_bytes() for p in (reg.published / good.run_id).iterdir()} == before
    assert reg.get(good.run_id).status is RunStatus.COMPLETE
    assert reg.latest_complete("validation").run_id == good.run_id  # type: ignore[union-attr]


def test_exception_inside_a_run_marks_it_failed(tmp_path: Path) -> None:
    reg = make(tmp_path)
    with pytest.raises(ValueError), start(reg) as h:
        h.write_artifact("a.csv", "x")
        raise ValueError("engine error\nwith details")
    rec = reg.get(h.run_id)
    assert rec.status is RunStatus.FAILED and rec.failure == "ValueError: engine error with details"
    assert (reg.staging / h.run_id / "a.csv").exists()  # kept for diagnosis


def test_failure_reason_is_single_line_and_bounded(tmp_path: Path) -> None:
    reg = make(tmp_path)
    rec = start(reg).fail("x\n" * 1000)
    assert rec.failure is not None
    assert "\n" not in rec.failure and len(rec.failure) <= 500


# ── 9. corrupt history ──


def test_tampered_index_is_detected(tmp_path: Path) -> None:
    reg = make(tmp_path)
    publish(reg)
    lines = reg.index_path.read_text(encoding="utf-8").splitlines()
    lines[0] = lines[0].replace('"validation"', '"ml"')
    reg.index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(RegistryCorruptError, match="hash chain broken"):
        reg.records()
    with pytest.raises(RegistryCorruptError):
        start(reg)  # writers refuse to append to a corrupt history


def test_truncated_or_invalid_index_line_is_detected(tmp_path: Path) -> None:
    reg = make(tmp_path)
    publish(reg)
    with open(reg.index_path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 3, "half')
    with pytest.raises(RegistryCorruptError, match="not valid JSON"):
        reg.records()


def _forge(reg: RunRegistry, body: dict[str, Any]) -> None:
    """Append a well-chained but semantically invalid event (bypassing the writer)."""
    rr._Index(reg.index_path).append([body])


@pytest.mark.parametrize(
    "body",
    [
        {
            "event": "completed",
            "run_id": "ml-20261011T120000000000Z-0000abcd",
            "at": "x",
            "manifest_sha256": "0" * 64,
        },
        {"event": "started", "run_id": "bad id", "kind": "ml", "at": "x"},
        {
            "event": "started",
            "run_id": "ml-20261011T120000000000Z-0000abcd",
            "kind": "trading",
            "at": "x",
        },
        {"event": "deleted", "run_id": "ml-20261011T120000000000Z-0000abcd", "at": "x"},
    ],
)
def test_inconsistent_history_is_detected(tmp_path: Path, body: dict[str, Any]) -> None:
    reg = make(tmp_path)
    reg.root.mkdir(parents=True)
    _forge(reg, body)
    with pytest.raises(RegistryCorruptError):
        reg.records()


def test_duplicate_terminal_event_in_history_is_detected(tmp_path: Path) -> None:
    reg = make(tmp_path)
    rec = publish(reg)
    _forge(reg, {"event": "failed", "run_id": rec.run_id, "at": "x", "reason": "late"})
    with pytest.raises(RegistryCorruptError, match="without a start"):
        reg.records()


# ── 10. concurrency ──


def test_concurrent_registrations_keep_a_consistent_history(tmp_path: Path) -> None:
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            reg = make(tmp_path)  # separate registry objects, as separate callers would have
            for _ in range(4):
                publish(reg)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    recs = make(tmp_path).records()
    assert len(recs) == 12 and all(r.status is RunStatus.COMPLETE for r in recs)
    seqs = [
        json.loads(x)["seq"]
        for x in (tmp_path / "registry" / "index.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert seqs == list(range(1, 25))  # 12 started + 12 completed, contiguous chain


def test_transient_lock_file_sharing_errors_do_not_leave_the_lock_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows: the lock file cannot be created while a delete is pending, nor deleted while
    another process has it open to read its owner. Both are retried, never leaked."""
    reg = make(tmp_path)
    real_acquire, real_release = PipelineLock.acquire, PipelineLock.release
    calls = {"acquire": 0, "release": 0}

    def flaky_acquire(self: PipelineLock) -> None:
        calls["acquire"] += 1
        if calls["acquire"] == 1:
            raise PermissionError(13, "delete pending")
        real_acquire(self)

    def flaky_release(self: PipelineLock) -> None:
        calls["release"] += 1
        if calls["release"] <= 2:
            raise PermissionError(13, "in use by another process")
        real_release(self)

    monkeypatch.setattr(PipelineLock, "acquire", flaky_acquire)
    monkeypatch.setattr(PipelineLock, "release", flaky_release)
    h = start(reg)
    assert calls == {"acquire": 2, "release": 3}
    assert not (reg.root / "pipeline.lock").exists()
    h.write_artifact("a.csv", "x")
    assert h.publish().status is RunStatus.COMPLETE


def test_concurrent_processes_keep_a_consistent_history(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    code = (
        "import sys; from pathlib import Path; "
        "from app.research_runs.registry import RunRegistry, CodeVersion, UNKNOWN; "
        "reg = RunRegistry(Path(sys.argv[1]), code_version=lambda: CodeVersion('x', UNKNOWN, None),"
        " lock_timeout=60)\n"
        "for _ in range(5):\n"
        "    h = reg.start('ml', config_fingerprint=UNKNOWN, data_fingerprint=UNKNOWN,"
        " dataset_key=UNKNOWN, parameters={}, required_artifacts=('a.csv',))\n"
        "    h.write_artifact('a.csv', 'x'); h.publish()\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-B", "-c", code, str(root)])  # noqa: S603
        for _ in range(3)
    ]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0]
    recs = RunRegistry(root).records()
    assert len(recs) == 15 and all(r.status is RunStatus.COMPLETE for r in recs)
    assert not (root / "pipeline.lock").exists()


def test_busy_lock_times_out_without_writing(tmp_path: Path) -> None:
    reg = make(tmp_path, lock_timeout=0.3)
    reg.root.mkdir(parents=True)
    holder = PipelineLock(reg.root, purpose="test-holder")
    holder.acquire()
    try:
        with pytest.raises(RegistryBusyError):
            start(reg)
        assert not reg.index_path.exists()
    finally:
        holder.release()
    assert publish(reg).status is RunStatus.COMPLETE  # works once the lock is free


# ── 11. transitions ──


def test_forbidden_transitions(tmp_path: Path) -> None:
    reg = make(tmp_path)
    h = start(reg)
    h.write_artifact("a.csv", "x")
    twin = copy.copy(h)  # another handle on the same run (e.g. a second caller)
    h.publish()
    for action in (h.publish, lambda: h.fail("late"), lambda: h.write_artifact("b.csv", "x")):
        with pytest.raises(InvalidTransitionError):
            action()
    with pytest.raises(InvalidTransitionError, match="already completed"):
        twin.fail("contradiction")  # rejected by the history, not only by the object
    g = start(reg)
    g.fail("first")
    g2 = copy.copy(g)
    g2.ended = None
    with pytest.raises(InvalidTransitionError, match="already failed"):
        g2.publish()
    assert reg.get(h.run_id).status is RunStatus.COMPLETE
    assert reg.get(g.run_id).status is RunStatus.FAILED


# ── reading never writes; import boundary; code version ──


def test_reading_a_missing_registry_creates_nothing(tmp_path: Path) -> None:
    reg = RunRegistry(tmp_path / "absent")
    assert reg.records() == [] and reg.latest_complete("ml") is None and reg.unregistered() == []
    assert not (tmp_path / "absent").exists()


def test_unregistered_directories_are_reported_not_deleted(tmp_path: Path) -> None:
    reg = make(tmp_path)
    publish(reg)
    (reg.staging / "stray").mkdir(parents=True)
    assert reg.unregistered() == ["staging/stray"]
    assert (reg.staging / "stray").exists()


def test_reader_import_loads_no_operational_alerts_or_ml_module() -> None:
    code = (
        "import json, sys; from pathlib import Path; "
        "from app.research_runs.registry import RunRegistry; "
        "RunRegistry(Path('does-not-exist-xyz')).records(); "
        "p = ('app.operations', 'app.alerts', 'app.broker', 'app.paper', 'app.ml', "
        "'app.validation', 'sklearn', 'streamlit', 'app.dashboard'); "
        "print(json.dumps(sorted(k for k in sys.modules if k.startswith(p))))"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-B", "-c", code], capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout.strip().splitlines()[-1]) == []


def _inside_git_checkout(path: Path) -> bool:
    return any((p / ".git").exists() for p in (path, *path.parents))


def test_code_version_is_detected_or_explicitly_unknown(tmp_path: Path) -> None:
    unknown = rr.detect_code_version(tmp_path)  # not a git repository
    assert unknown.git_commit == UNKNOWN and unknown.git_dirty is None
    code_root = Path(rr.__file__).resolve().parents[2]
    here = rr.detect_code_version()
    if _inside_git_checkout(code_root):  # normal checkout: commit and dirty state are known
        assert here.git_commit.state == "known" and re.fullmatch(
            r"[0-9a-f]{40}", here.git_commit.value or ""
        )
        assert isinstance(here.git_dirty, bool)
    else:  # exported copy without .git: explicitly unknown, never guessed
        assert here.git_commit == UNKNOWN and here.git_dirty is None


def test_index_entries_use_the_shared_hash(tmp_path: Path) -> None:
    reg = make(tmp_path)
    publish(reg)
    for line in reg.index_path.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        assert e["hash"] == entry_hash(e)
