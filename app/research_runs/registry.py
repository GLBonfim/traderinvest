"""Registry of offline research runs: staged writing, atomic publication, SHA-256 verification
and an append-only hash-chained history.

Layout under a root directory (the caller chooses it; nothing is created on import or read):

    index.jsonl          append-only SHA-256 chain of events: started / completed / failed
    staging/<run_id>/    artifacts of a run being written (never read as a result)
    runs/<run_id>/       published runs: artifacts + run.json (manifest), never rewritten
    pipeline.lock        writer lock (O_CREAT|O_EXCL, app.operations.lock.PipelineLock)

Publication order: every artifact is written to staging -> the manifest (format version, kind,
fingerprints, code version, parameters, and the size and SHA-256 of every artifact) is written
atomically -> the staged run is verified -> the directory is renamed into `runs/` -> a
`completed` event carrying the manifest's SHA-256 is appended. A run is COMPLETE only when
all of these hold and every artifact still verifies; a manifest alone is never sufficient.

Derived states (never stored as "complete" by assumption):
    complete          completed event + published directory that verifies
    integrity_failed  completed event but missing/extra/altered files or an invalid manifest
    failed            failed event (its staging directory is kept for diagnosis)
    incomplete        no terminal event: still running, or interrupted (also when the directory
                      was published but the completed event was never written)

Reads never take the lock and never write. Nothing is ever deleted or repaired automatically.
"""

import hashlib
import json
import os
import re
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Literal

from app import __version__
from app.core.integrity import HashChain, atomic_write

FORMAT_VERSION = 1
KINDS = ("validation", "ml")
RunKind = Literal["validation", "ml"]
MANIFEST = "run.json"
_RUN_ID = re.compile(r"^(validation|ml)-\d{8}T\d{12}Z-[0-9a-f]{8}$")
_ARTIFACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FINGERPRINT = re.compile(r"^[A-Za-z0-9:_.+-]{1,128}$")
_MAX_REASON = 500


class RegistryError(RuntimeError):
    """Base error of the run registry."""


class RegistryCorruptError(RegistryError):
    """The history (index) is corrupt or inconsistent; nothing was modified."""


class InvalidTransitionError(RegistryError):
    """The requested state change is not allowed (e.g. completing a failed run)."""


class RegistryBusyError(RegistryError):
    """The writer lock could not be acquired in time."""


class ManifestError(RegistryError):
    """A manifest is missing fields, has wrong types or an unsupported format version."""


class _Index(HashChain):
    error = RegistryCorruptError


class RunStatus(StrEnum):
    COMPLETE = "complete"
    INTEGRITY_FAILED = "integrity_failed"
    FAILED = "failed"
    INCOMPLETE = "incomplete"


# ── value objects ──


@dataclass(frozen=True)
class Fingerprint:
    """A fingerprint that is known, explicitly unknown, or not applicable — never invented."""

    state: Literal["known", "unknown", "not_applicable"]
    value: str | None = None

    def __post_init__(self) -> None:
        if self.state == "known":
            if not isinstance(self.value, str) or not _FINGERPRINT.match(self.value):
                raise ManifestError(f"invalid fingerprint value {self.value!r}")
        elif self.state in ("unknown", "not_applicable"):
            if self.value is not None:
                raise ManifestError(f"a {self.state} fingerprint has no value")
        else:
            raise ManifestError(f"invalid fingerprint state {self.state!r}")

    @classmethod
    def known(cls, value: str) -> "Fingerprint":
        return cls("known", value)

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "value": self.value}

    @classmethod
    def from_dict(cls, d: Any) -> "Fingerprint":
        if not isinstance(d, dict) or set(d) != {"state", "value"}:
            raise ManifestError(f"invalid fingerprint {d!r}")
        return cls(d["state"], d["value"])


UNKNOWN = Fingerprint("unknown")
NOT_APPLICABLE = Fingerprint("not_applicable")


@dataclass(frozen=True)
class CodeVersion:
    app_version: str
    git_commit: Fingerprint
    git_dirty: bool | None  # None = unknown (git unavailable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_version": self.app_version,
            "git_commit": self.git_commit.to_dict(),
            "git_dirty": self.git_dirty,
        }

    @classmethod
    def from_dict(cls, d: Any) -> "CodeVersion":
        if not isinstance(d, dict) or set(d) != {"app_version", "git_commit", "git_dirty"}:
            raise ManifestError(f"invalid code version {d!r}")
        if not isinstance(d["app_version"], str) or not d["app_version"]:
            raise ManifestError("invalid app_version")
        if d["git_dirty"] is not None and not isinstance(d["git_dirty"], bool):
            raise ManifestError("invalid git_dirty")
        return cls(d["app_version"], Fingerprint.from_dict(d["git_commit"]), d["git_dirty"])


def detect_code_version(repo: Path | None = None) -> CodeVersion:
    """App version and git HEAD; `git_dirty` counts any modified or untracked file. Unknown
    (never guessed) when git is unavailable. Read-only git commands."""
    cwd = repo or Path(__file__).resolve().parents[2]

    def git(*args: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed read-only git commands
            ["git", "--no-optional-locks", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout

    try:
        commit = Fingerprint.known(git("rev-parse", "HEAD").strip())
        dirty: bool | None = bool(git("status", "--porcelain").strip())
    except (OSError, subprocess.SubprocessError, ManifestError):
        commit, dirty = UNKNOWN, None
    return CodeVersion(__version__, commit, dirty)


@dataclass(frozen=True)
class Artifact:
    path: str
    sha256: str
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sha256": self.sha256, "bytes": self.bytes}

    @classmethod
    def from_dict(cls, d: Any) -> "Artifact":
        if not isinstance(d, dict) or set(d) != {"path", "sha256", "bytes"}:
            raise ManifestError(f"invalid artifact {d!r}")
        if (
            not isinstance(d["path"], str)
            or not _ARTIFACT.match(d["path"])
            or d["path"] == MANIFEST
        ):
            raise ManifestError(f"invalid artifact path {d.get('path')!r}")
        if not isinstance(d["sha256"], str) or not _SHA256.match(d["sha256"]):
            raise ManifestError(f"invalid sha256 for {d['path']}")
        if not isinstance(d["bytes"], int) or isinstance(d["bytes"], bool) or d["bytes"] < 0:
            raise ManifestError(f"invalid size for {d['path']}")
        return cls(d["path"], d["sha256"], d["bytes"])


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    kind: str
    started_at: str
    finished_at: str
    config_fingerprint: Fingerprint
    data_fingerprint: Fingerprint
    dataset_key: Fingerprint
    code: CodeVersion
    parameters: dict[str, Any]
    artifacts: tuple[Artifact, ...]
    format_version: int = FORMAT_VERSION
    status: str = "complete"

    KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "format_version",
            "run_id",
            "kind",
            "status",
            "started_at",
            "finished_at",
            "config_fingerprint",
            "data_fingerprint",
            "dataset_key",
            "code",
            "parameters",
            "artifacts",
        }
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "run_id": self.run_id,
            "kind": self.kind,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "config_fingerprint": self.config_fingerprint.to_dict(),
            "data_fingerprint": self.data_fingerprint.to_dict(),
            "dataset_key": self.dataset_key.to_dict(),
            "code": self.code.to_dict(),
            "parameters": self.parameters,
            "artifacts": [a.to_dict() for a in self.artifacts],
        }

    @classmethod
    def from_dict(cls, d: Any) -> "RunManifest":
        if not isinstance(d, dict):
            raise ManifestError("manifest is not an object")
        if set(d) != cls.KEYS:
            missing, extra = sorted(cls.KEYS - set(d)), sorted(set(d) - cls.KEYS)
            raise ManifestError(f"manifest keys: missing {missing}, unexpected {extra}")
        if d["format_version"] != FORMAT_VERSION:
            raise ManifestError(f"unsupported format_version {d['format_version']!r}")
        if not isinstance(d["run_id"], str) or not _RUN_ID.match(d["run_id"]):
            raise ManifestError(f"invalid run_id {d['run_id']!r}")
        if d["kind"] not in KINDS or not d["run_id"].startswith(d["kind"] + "-"):
            raise ManifestError(f"invalid kind {d['kind']!r}")
        if d["status"] != "complete":
            raise ManifestError(f"a published manifest must be complete, not {d['status']!r}")
        for k in ("started_at", "finished_at"):
            try:
                datetime.fromisoformat(d[k])
            except (TypeError, ValueError) as exc:
                raise ManifestError(f"invalid {k}") from exc
        if not isinstance(d["parameters"], dict):
            raise ManifestError("parameters must be an object")
        if not isinstance(d["artifacts"], list) or not d["artifacts"]:
            raise ManifestError("a run needs at least one artifact")
        artifacts = tuple(Artifact.from_dict(a) for a in d["artifacts"])
        names = [a.path for a in artifacts]
        if names != sorted(set(names)):
            raise ManifestError("artifact paths must be unique and sorted")
        return cls(
            run_id=d["run_id"],
            kind=d["kind"],
            started_at=d["started_at"],
            finished_at=d["finished_at"],
            config_fingerprint=Fingerprint.from_dict(d["config_fingerprint"]),
            data_fingerprint=Fingerprint.from_dict(d["data_fingerprint"]),
            dataset_key=Fingerprint.from_dict(d["dataset_key"]),
            code=CodeVersion.from_dict(d["code"]),
            parameters=d["parameters"],
            artifacts=artifacts,
        )


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    kind: str
    status: RunStatus
    started_at: str
    finished_at: str | None
    manifest: RunManifest | None = None
    failure: str | None = None
    problems: tuple[str, ...] = field(default_factory=tuple)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _sanitize(reason: str) -> str:
    one_line = " ".join(str(reason).split())
    return one_line[:_MAX_REASON] or "unspecified"


# ── registry ──


_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _process_lock(root: Path) -> threading.Lock:
    key = os.path.normcase(str(root.resolve()))
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


def _release(lock: Any) -> None:
    """Releases the file lock once: PipelineLock.release() already retries transient sharing
    errors within its own bound. If it still fails, the lock stays held by this process (it is
    never deleted unconfirmed); it becomes breakable as stale when this process exits."""
    from app.operations.lock import LockReleaseError

    try:
        lock.release()
    except LockReleaseError as exc:
        raise RegistryError(
            f"registry lock could not be released and is still held by this process: {exc}"
        ) from exc


class RunRegistry:
    def __init__(
        self,
        root: Path,
        *,
        lock_timeout: float = 10.0,
        clock: Callable[[], datetime] | None = None,
        code_version: Callable[[], CodeVersion] = detect_code_version,
    ) -> None:
        self.root = Path(root)
        self.index_path = self.root / "index.jsonl"
        self.staging = self.root / "staging"
        self.published = self.root / "runs"
        self.lock_timeout = lock_timeout
        self.clock = clock or (lambda: datetime.now(UTC))
        self.code_version = code_version

    # ── reading (no lock, no writes) ──

    def _events(self) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []
        events = _Index(self.index_path).entries
        seen: dict[str, str] = {}  # run_id -> last event
        for e in events:
            run_id, kind = e.get("run_id"), e.get("event")
            if not isinstance(run_id, str) or not _RUN_ID.match(run_id):
                raise RegistryCorruptError(f"index seq {e.get('seq')}: invalid run_id")
            if kind == "started":
                if run_id in seen or e.get("kind") not in KINDS:
                    raise RegistryCorruptError(f"index seq {e['seq']}: invalid start of {run_id}")
            elif kind in ("completed", "failed"):
                if seen.get(run_id) != "started":
                    raise RegistryCorruptError(f"index seq {e['seq']}: {kind} without a start")
                if kind == "completed" and not _SHA256.match(str(e.get("manifest_sha256"))):
                    raise RegistryCorruptError(f"index seq {e['seq']}: invalid manifest hash")
            else:
                raise RegistryCorruptError(f"index seq {e.get('seq')}: unknown event {kind!r}")
            seen[run_id] = kind
        return events

    def records(self) -> list[RunRecord]:
        started: dict[str, dict[str, Any]] = {}
        terminal: dict[str, dict[str, Any]] = {}
        for e in self._events():
            (started if e["event"] == "started" else terminal)[e["run_id"]] = e
        out = []
        for run_id, s in started.items():
            t = terminal.get(run_id)
            if t is None:
                problems = ["no completion record (still running or interrupted)"]
                if (self.published / run_id).exists():
                    problems.append("published directory present without a completed event")
                out.append(
                    RunRecord(
                        run_id,
                        s["kind"],
                        RunStatus.INCOMPLETE,
                        s["at"],
                        None,
                        None,
                        None,
                        tuple(problems),
                    )
                )
            elif t["event"] == "failed":
                out.append(
                    RunRecord(
                        run_id, s["kind"], RunStatus.FAILED, s["at"], t["at"], None, t["reason"]
                    )
                )
            else:
                manifest, issues = self._verify(run_id, s, t["manifest_sha256"])
                status = RunStatus.COMPLETE if not issues else RunStatus.INTEGRITY_FAILED
                out.append(
                    RunRecord(run_id, s["kind"], status, s["at"], t["at"], manifest, None, issues)
                )
        return out

    def get(self, run_id: str) -> RunRecord:
        for r in self.records():
            if r.run_id == run_id:
                return r
        raise KeyError(run_id)

    def latest_complete(
        self,
        kind: RunKind,
        *,
        config_fingerprint: Fingerprint | None = None,
        data_fingerprint: Fingerprint | None = None,
    ) -> RunRecord | None:
        """The most recently started run of `kind` that is COMPLETE and verifies (optionally
        matching known fingerprints). Incomplete, failed or altered runs are never returned."""
        for r in reversed(self.records()):
            if r.kind != kind or r.status is not RunStatus.COMPLETE or r.manifest is None:
                continue
            if (
                config_fingerprint is not None
                and r.manifest.config_fingerprint != config_fingerprint
            ):
                continue
            if data_fingerprint is not None and r.manifest.data_fingerprint != data_fingerprint:
                continue
            return r
        return None

    def unregistered(self) -> list[str]:
        """Directories under staging/ or runs/ that the index does not know (diagnosis only)."""
        known = {e["run_id"] for e in self._events()}
        found = []
        for base in (self.staging, self.published):
            if base.exists():
                found += [
                    f"{base.name}/{p.name}" for p in sorted(base.iterdir()) if p.name not in known
                ]
        return found

    def _verify(
        self, run_id: str, started: dict[str, Any], manifest_sha: str
    ) -> tuple[RunManifest | None, tuple[str, ...]]:
        d = self.published / run_id
        mpath = d / MANIFEST
        if not mpath.is_file():
            return None, ("published manifest missing",)
        problems: list[str] = []
        if sha256_file(mpath) != manifest_sha:
            problems.append("manifest does not match the hash recorded at completion")
        try:
            manifest = RunManifest.from_dict(json.loads(mpath.read_text(encoding="utf-8")))
        except (ValueError, ManifestError) as exc:
            return None, (*problems, f"invalid manifest: {exc}")
        if manifest.run_id != run_id or manifest.kind != started["kind"]:
            problems.append("manifest identity differs from the index")
        expected = {a.path: a for a in manifest.artifacts}
        present = {p.name for p in d.iterdir() if p.name != MANIFEST}
        problems += [f"artifact missing: {n}" for n in sorted(set(expected) - present)]
        problems += [f"unexpected file: {n}" for n in sorted(present - set(expected))]
        for n in sorted(set(expected) & present):
            p, a = d / n, expected[n]
            if p.stat().st_size != a.bytes or sha256_file(p) != a.sha256:
                problems.append(f"artifact altered: {n}")
        missing_required = sorted(set(started.get("required_artifacts", [])) - set(expected))
        problems += [f"required artifact not recorded: {n}" for n in missing_required]
        return manifest, tuple(problems)

    # ── writing (under the lock) ──

    @contextmanager
    def _locked(self) -> Iterator[None]:
        # Imported here so that read-only users of this module never load app.operations.
        from app.operations.lock import LockHeldError, PipelineLock

        self.root.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.lock_timeout
        # Writers of the same process are serialised here first: on Windows a lock file cannot
        # be deleted while another thread has it open to read its owner, so in-process contention
        # on the file lock alone could leave it behind.
        local = _process_lock(self.root)
        if not local.acquire(timeout=max(self.lock_timeout, 0.0)):
            raise RegistryBusyError("registry lock busy (another writer in this process)")
        try:
            lock = PipelineLock(self.root, purpose="research-run-registry")
            while True:
                try:
                    lock.acquire()
                    break
                except LockHeldError as exc:  # contention only; other OS errors propagate as is
                    if time.monotonic() >= deadline:
                        raise RegistryBusyError(f"registry lock busy: {exc}") from exc
                    time.sleep(0.02)
            try:
                yield
            finally:
                _release(lock)
        finally:
            local.release()

    def _append(self, body: dict[str, Any]) -> None:
        index = _Index(self.index_path)  # re-read under the lock: detects corruption, keeps seq
        index.append([body])

    def _terminal(self, run_id: str) -> str | None:
        for e in self._events():
            if e["run_id"] == run_id and e["event"] in ("completed", "failed"):
                return str(e["event"])
        return None

    def new_run_id(self, kind: RunKind) -> str:
        if kind not in KINDS:
            raise RegistryError(f"unknown run kind {kind!r}")
        stamp = self.clock().astimezone(UTC).strftime("%Y%m%dT%H%M%S%f")
        return f"{kind}-{stamp}Z-{uuid.uuid4().hex[:8]}"

    def start(
        self,
        kind: RunKind,
        *,
        config_fingerprint: Fingerprint,
        data_fingerprint: Fingerprint,
        dataset_key: Fingerprint,
        parameters: dict[str, Any],
        required_artifacts: tuple[str, ...],
    ) -> "RunHandle":
        if kind not in KINDS:
            raise RegistryError(f"unknown run kind {kind!r}")
        for fp in (config_fingerprint, data_fingerprint, dataset_key):
            if not isinstance(fp, Fingerprint):
                raise ManifestError("fingerprints must be Fingerprint values (known/unknown/n.a.)")
        try:  # strict: no default=str, so parameters are recorded exactly or rejected
            json.dumps(parameters, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ManifestError(f"parameters are not JSON-serializable: {exc}") from exc
        if not required_artifacts:
            raise ManifestError("at least one required artifact must be declared")
        for n in required_artifacts:
            if not _ARTIFACT.match(n) or n == MANIFEST:
                raise ManifestError(f"invalid artifact name {n!r}")
        code = self.code_version()
        with self._locked():
            run_id = self.new_run_id(kind)
            (self.staging / run_id).mkdir(parents=True, exist_ok=False)
            started_at = self.clock().isoformat()
            self._append(
                {
                    "event": "started",
                    "run_id": run_id,
                    "kind": kind,
                    "at": started_at,
                    "format_version": FORMAT_VERSION,
                    "required_artifacts": sorted(required_artifacts),
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                }
            )
        return RunHandle(
            self, run_id, kind, started_at, config_fingerprint, data_fingerprint, dataset_key,
            code, dict(parameters), tuple(sorted(required_artifacts)),
        )  # fmt: skip


class RunHandle:
    """A run being written. Artifacts go to staging; `publish()` or `fail()` ends it."""

    def __init__(
        self,
        registry: RunRegistry,
        run_id: str,
        kind: str,
        started_at: str,
        config_fingerprint: Fingerprint,
        data_fingerprint: Fingerprint,
        dataset_key: Fingerprint,
        code: CodeVersion,
        parameters: dict[str, Any],
        required: tuple[str, ...],
    ) -> None:
        self.registry = registry
        self.run_id = run_id
        self.kind = kind
        self.started_at = started_at
        self.config_fingerprint = config_fingerprint
        self.data_fingerprint = data_fingerprint
        self.dataset_key = dataset_key
        self.code = code
        self.parameters = parameters
        self.required = required
        self.dir = registry.staging / run_id
        self.ended: str | None = None

    def _check_open(self) -> None:
        if self.ended is not None:
            raise InvalidTransitionError(f"run {self.run_id} already {self.ended}")

    def write_artifact(self, name: str, data: bytes | str) -> None:
        self._check_open()
        if not _ARTIFACT.match(name) or name == MANIFEST or name.endswith(".tmp"):
            raise ManifestError(f"invalid artifact name {name!r}")
        target = self.dir / name
        if isinstance(data, str):
            atomic_write(target, data)
        else:
            _atomic_write_bytes(target, data)

    def publish(self) -> RunRecord:
        self._check_open()
        reg = self.registry
        with reg._locked():
            terminal = reg._terminal(self.run_id)
            if terminal:
                raise InvalidTransitionError(f"run {self.run_id} already {terminal}")
            files = sorted(p for p in self.dir.iterdir())
            leftovers = [p.name for p in files if p.suffix == ".tmp" or p.name == MANIFEST]
            if leftovers:
                raise RegistryError(f"staging contains unexpected files: {leftovers}")
            names = [p.name for p in files]
            missing = sorted(set(self.required) - set(names))
            if missing:
                raise RegistryError(f"required artifacts missing: {missing}")
            artifacts = tuple(Artifact(p.name, sha256_file(p), p.stat().st_size) for p in files)
            manifest = RunManifest(
                run_id=self.run_id,
                kind=self.kind,
                started_at=self.started_at,
                finished_at=reg.clock().isoformat(),
                config_fingerprint=self.config_fingerprint,
                data_fingerprint=self.data_fingerprint,
                dataset_key=self.dataset_key,
                code=self.code,
                parameters=self.parameters,
                artifacts=artifacts,
            )
            RunManifest.from_dict(manifest.to_dict())  # validate what will be published
            atomic_write(
                self.dir / MANIFEST, json.dumps(manifest.to_dict(), indent=2, sort_keys=True)
            )
            for a in artifacts:  # re-verify staging right before publication
                if sha256_file(self.dir / a.path) != a.sha256:
                    raise RegistryError(f"artifact changed while publishing: {a.path}")
            manifest_sha = sha256_file(self.dir / MANIFEST)
            reg.published.mkdir(parents=True, exist_ok=True)
            target = reg.published / self.run_id
            if target.exists():
                raise RegistryError(f"refusing to overwrite published run {self.run_id}")
            os.rename(self.dir, target)  # atomic on one volume; fails if the target exists
            reg._append(
                {
                    "event": "completed",
                    "run_id": self.run_id,
                    "at": manifest.finished_at,
                    "manifest_sha256": manifest_sha,
                }
            )
        self.ended = "completed"
        return reg.get(self.run_id)

    def fail(self, reason: str) -> RunRecord:
        self._check_open()
        reg = self.registry
        with reg._locked():
            terminal = reg._terminal(self.run_id)
            if terminal:
                raise InvalidTransitionError(f"run {self.run_id} already {terminal}")
            reg._append(
                {
                    "event": "failed",
                    "run_id": self.run_id,
                    "at": reg.clock().isoformat(),
                    "reason": _sanitize(reason),
                }
            )
        self.ended = "failed"
        return reg.get(self.run_id)

    def __enter__(self) -> "RunHandle":
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object
    ) -> None:
        if exc_type is not None and self.ended is None:
            self.fail(f"{exc_type.__name__}: {exc}")
