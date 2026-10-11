"""PipelineLock: ownership, mutual exclusion across threads and processes, bounded retries for
Windows sharing violations, no abandoned locks, stale-lock breaking and history integrity under
concurrency. Temporary directories only."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from app.core.integrity import HashChain
from app.operations import lock as lock_mod
from app.operations.lock import LockHeldError, LockReleaseError, PipelineLock

REPO = Path(__file__).resolve().parents[3]

ACQUIRE_LOOP = """
import json, os, sys, time
from pathlib import Path
from app.operations.lock import LockHeldError, PipelineLock

def acquire(root, purpose, patience=60.0):
    lock = PipelineLock(root, purpose=purpose)
    end = time.monotonic() + patience
    while True:
        try:
            lock.acquire()
            return lock
        except LockHeldError:
            if time.monotonic() > end:
                return None
            time.sleep(0.001)
"""

CHILD_MARKER = (
    ACQUIRE_LOOP
    + """
root, iters = Path(sys.argv[1]), int(sys.argv[2])
c = {"ok": 0, "overlap": 0, "timeout": 0}
for _ in range(iters):
    lock = acquire(root, "test-child")
    if lock is None:
        c["timeout"] += 1
        continue
    try:
        try:
            fd = os.open(root / "inside", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            c["overlap"] += 1
        else:
            os.close(fd)
            time.sleep(0.001)
            os.remove(root / "inside")
            c["ok"] += 1
    finally:
        lock.release()
print(json.dumps(c))
"""
)

CHILD_CHAIN = (
    ACQUIRE_LOOP
    + """
from app.core.integrity import HashChain
root, tag, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
for i in range(n):
    lock = acquire(root, "test-chain")
    assert lock is not None, "timeout"
    try:
        HashChain(root / "history.jsonl").append([{"writer": tag, "i": i}])
    finally:
        lock.release()
"""
)

CHILD_HOLDER = """
import sys
from pathlib import Path
from app.operations.lock import PipelineLock
lock = PipelineLock(Path(sys.argv[1]), purpose="test-holder")
lock.acquire()
import os
print("held", os.getpid(), flush=True)
sys.stdin.readline()
lock.release()
print("released", flush=True)
"""


def spawn(code: str, *args: object, **kw: Any) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-B", "-c", code, *map(str, args)], cwd=REPO, text=True, **kw
    )


class SafeCounts:
    """Thread-safe tally (Counter += is not atomic across threads)."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self._guard = threading.Lock()

    def add(self, key: str) -> None:
        with self._guard:
            self.counts[key] += 1


def thread_worker(root: Path, iters: int, tally: SafeCounts) -> None:
    for _ in range(iters):
        lock = PipelineLock(root, purpose="test-thread")
        end = time.monotonic() + 30
        while True:
            try:
                lock.acquire()
                break
            except LockHeldError:
                if time.monotonic() > end:
                    tally.add("timeout")
                    return
                time.sleep(0.001)
            except Exception as exc:  # counted, never hidden
                tally.add(f"acquire:{type(exc).__name__}")
                return
        try:
            try:
                fd = os.open(root / "inside", os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                tally.add("overlap")
            else:
                os.close(fd)
                lock.owner()  # a concurrent reader: the cause of the original race
                time.sleep(0.0005)
                os.remove(root / "inside")
                tally.add("ok")
        finally:
            try:
                lock.release()
            except Exception as exc:
                tally.add(f"release:{type(exc).__name__}")


def run_threads(root: Path, threads: int, iters: int) -> Counter[str]:
    tally = SafeCounts()
    readers_stop = threading.Event()

    def reader() -> None:  # keeps opening the lock file, like the dashboard / registry readers
        probe = PipelineLock(root)
        while not readers_stop.is_set():
            probe.owner()

    r = threading.Thread(target=reader)
    r.start()
    ts = [threading.Thread(target=thread_worker, args=(root, iters, tally)) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    readers_stop.set()
    r.join()
    return tally.counts


# ── 1/5. normal acquire / release / re-acquire ──


def test_acquire_and_release(tmp_path: Path) -> None:
    lock = PipelineLock(tmp_path / "ops", purpose="unit")
    lock.acquire()
    owner = json.loads(lock.path.read_text(encoding="utf-8"))
    assert lock.held and owner["lock_id"] == lock.lock_id and owner["pid"] == os.getpid()
    assert owner["purpose"] == "unit" and owner["host"] == socket.gethostname()
    assert lock.owner() == owner
    lock.release()
    assert not lock.held and not lock.path.exists() and lock.owner() is None
    assert lock.mutex_path.exists()  # persistent mutex file, never deleted
    lock.release()  # idempotent


def test_release_then_reacquire(tmp_path: Path) -> None:
    a = PipelineLock(tmp_path)
    b = PipelineLock(tmp_path)
    a.acquire()
    with pytest.raises(LockHeldError) as info:
        b.acquire()
    assert info.value.owner["lock_id"] == a.lock_id
    a.release()
    b.acquire()
    assert json.loads(b.path.read_text(encoding="utf-8"))["lock_id"] == b.lock_id
    b.release()
    a.acquire()  # the same instance can be acquired again
    a.release()
    assert not a.path.exists()


def test_second_acquire_of_a_held_instance_is_refused(tmp_path: Path) -> None:
    a = PipelineLock(tmp_path)
    a.acquire()
    with pytest.raises(LockHeldError):
        a.acquire()
    a.release()


# ── 6. exception inside the protected operation ──


def test_exception_inside_the_protected_block_releases(tmp_path: Path) -> None:
    with pytest.raises(ZeroDivisionError), PipelineLock(tmp_path) as held:
        assert held.held
        _ = 1 / 0
    assert not (tmp_path / "pipeline.lock").exists()
    with PipelineLock(tmp_path):
        pass


# ── 2/11. threads, with a concurrent reader; repeated stress with explicit counts ──


def test_threads_never_overlap_and_never_leak(tmp_path: Path) -> None:
    counts = run_threads(tmp_path, threads=4, iters=25)
    assert counts == Counter({"ok": 100})
    assert not (tmp_path / "pipeline.lock").exists()


def test_repeated_thread_stress(tmp_path: Path) -> None:
    total: Counter[str] = Counter()
    leaked = 0
    for n in range(5):
        root = tmp_path / f"round{n}"
        total += run_threads(root, threads=6, iters=20)
        leaked += (root / "pipeline.lock").exists()
    assert (total, leaked) == (Counter({"ok": 600}), 0)  # 0 errors, 0 timeouts, 0 overlaps


# ── 3. processes ──


def test_processes_never_overlap_and_never_leak(tmp_path: Path) -> None:
    procs = [spawn(CHILD_MARKER, tmp_path, 25, stdout=subprocess.PIPE) for _ in range(4)]
    total: Counter[str] = Counter()
    for p in procs:
        out, _ = p.communicate(timeout=300)
        assert p.returncode == 0  # no unexpected exception in any process
        total += Counter(json.loads(out.strip().splitlines()[-1]))
    assert +total == Counter({"ok": 100})
    assert not (tmp_path / "pipeline.lock").exists()


# ── 4. a lock legitimately held by another process ──


def test_lock_held_by_another_process_is_respected(tmp_path: Path) -> None:
    holder = spawn(CHILD_HOLDER, tmp_path, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert holder.stdout is not None
        tag, pid = holder.stdout.readline().split()  # the venv launcher's child is the holder
        assert tag == "held"
        contender = PipelineLock(tmp_path)
        with pytest.raises(LockHeldError) as info:
            contender.acquire()
        assert info.value.owner["pid"] == int(pid) and info.value.owner["purpose"] == "test-holder"
        assert not contender.is_stale(info.value.owner)  # alive: never broken
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.stdout.readline().strip() == "released"
    finally:
        holder.communicate(timeout=60)
    contender.acquire()
    contender.release()


def test_registry_times_out_while_another_process_holds_the_lock(tmp_path: Path) -> None:
    from app.research_runs.registry import RegistryBusyError, RunRegistry

    root = tmp_path / "registry"
    root.mkdir()
    holder = spawn(CHILD_HOLDER, root, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        assert holder.stdout is not None and holder.stdout.readline().startswith("held")
        reg = RunRegistry(root, lock_timeout=0.3)
        t0 = time.monotonic()
        with pytest.raises(RegistryBusyError):
            reg.start(
                "ml", config_fingerprint=reg_fp(), data_fingerprint=reg_fp(), dataset_key=reg_fp(),
                parameters={}, required_artifacts=("a.csv",),
            )  # fmt: skip
        assert time.monotonic() - t0 < 5 and not (root / "index.jsonl").exists()
    finally:
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        holder.communicate(timeout=60)


def reg_fp() -> Any:
    from app.research_runs.registry import UNKNOWN

    return UNKNOWN


# ── 7/8. transient file access errors; no abandoned lock after recoverable failures ──


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing semantics")
def test_reader_holding_the_file_open_only_delays_release(tmp_path: Path) -> None:
    lock = PipelineLock(tmp_path)
    lock.acquire()
    fh = open(lock.path, encoding="utf-8")  # noqa: SIM115 - a real concurrent reader handle
    threading.Timer(0.2, fh.close).start()
    t0 = time.monotonic()
    lock.release()  # would raise PermissionError before Phase 5E
    assert 0.1 < time.monotonic() - t0 < lock_mod.RETRY_TIMEOUT_S
    assert not lock.held and not lock.path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing semantics")
def test_release_blocked_beyond_the_bound_is_reported_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lock_mod, "RETRY_TIMEOUT_S", 0.2)
    lock = PipelineLock(tmp_path)
    lock.acquire()
    with open(lock.path, encoding="utf-8"), pytest.raises(LockReleaseError):
        lock.release()
    assert lock.held and lock.path.exists()  # reported, still ours, not silently abandoned
    lock.release()  # the blocker went away: retry succeeds
    assert not lock.held and not lock.path.exists()


def test_transient_permission_errors_are_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open, real_unlink = os.open, Path.unlink
    fails = Counter({"open": 2, "unlink": 3})

    def flaky_open(path: Any, flags: int, *a: Any) -> int:
        if Path(path).name == "pipeline.lock" and fails["open"] > 0:
            fails["open"] -= 1
            raise PermissionError(13, "delete pending")
        return real_open(path, flags, *a)

    def flaky_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == "pipeline.lock" and fails["unlink"] > 0:
            fails["unlink"] -= 1
            raise PermissionError(13, "sharing violation")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(os, "open", flaky_open)
    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    lock = PipelineLock(tmp_path)
    lock.acquire()
    lock.release()
    assert +fails == Counter() and not (tmp_path / "pipeline.lock").exists()


def test_persistent_permission_error_on_create_is_not_disguised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lock_mod, "RETRY_TIMEOUT_S", 0.1)
    real_open = os.open

    def denied(path: Any, flags: int, *a: Any) -> int:
        if Path(path).name == "pipeline.lock":
            raise PermissionError(13, "access denied")
        return real_open(path, flags, *a)

    monkeypatch.setattr(os, "open", denied)
    lock = PipelineLock(tmp_path)
    with pytest.raises(PermissionError) as info:
        lock.acquire()
    assert not isinstance(info.value, LockReleaseError) and not lock.held


def test_failed_owner_write_leaves_no_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_fsync(fd: int) -> None:
        raise OSError(28, "no space left on device")

    monkeypatch.setattr(os, "fsync", broken_fsync)
    lock = PipelineLock(tmp_path)
    with pytest.raises(OSError, match="no space"):
        lock.acquire()
    monkeypatch.undo()
    assert not lock.held and not lock.path.exists()  # no empty, unbreakable lock file
    lock.acquire()
    lock.release()


def test_busy_mutex_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lock_mod, "MUTEX_TIMEOUT_S", 0.2)
    lock = PipelineLock(tmp_path)
    lock.acquire()
    entered, leave = threading.Event(), threading.Event()

    def wedge() -> None:
        with lock_mod._os_mutex(lock.mutex_path, 5):
            entered.set()
            leave.wait(5)

    t = threading.Thread(target=wedge)
    t.start()
    entered.wait(5)
    try:
        with pytest.raises(LockHeldError):
            PipelineLock(tmp_path).acquire()
        with pytest.raises(LockReleaseError, match="mutex busy"):
            lock.release()
        assert lock.held and lock.path.exists()
    finally:
        leave.set()
        t.join()
    lock.release()
    assert not lock.path.exists()


# ── 12. owner mismatch, stale and foreign locks ──


def test_release_never_removes_a_lock_it_does_not_own(tmp_path: Path) -> None:
    a = PipelineLock(tmp_path)
    a.acquire()
    foreign = {"lock_id": "someone-else", "host": socket.gethostname(), "pid": os.getpid()}
    a.path.write_text(json.dumps(foreign), encoding="utf-8")
    a.release()
    assert json.loads(a.path.read_text(encoding="utf-8")) == foreign and not a.held
    assert PipelineLock(tmp_path).force_release() == foreign  # explicit operator action only
    assert not a.path.exists()


def test_foreign_host_and_live_locks_are_never_broken(tmp_path: Path) -> None:
    for owner in (
        {"lock_id": "x", "pid": os.getpid(), "host": "other-host"},
        {"lock_id": "y", "pid": os.getpid(), "host": socket.gethostname()},
        {"unreadable": True},
    ):
        (tmp_path / "pipeline.lock").write_text(json.dumps(owner), encoding="utf-8")
        with pytest.raises(LockHeldError):
            PipelineLock(tmp_path).acquire()
        assert json.loads((tmp_path / "pipeline.lock").read_text(encoding="utf-8")) == owner


def _dead_pid() -> int:
    out = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True
    )
    return int(out.stdout)


def test_stale_lock_is_broken_by_exactly_one_contender(tmp_path: Path) -> None:
    dead = _dead_pid()
    doubles, errors = 0, Counter[str]()
    for n in range(15):
        root = tmp_path / f"r{n}"
        root.mkdir()
        stale = {"lock_id": "old", "pid": dead, "host": socket.gethostname()}
        (root / "pipeline.lock").write_text(json.dumps(stale), encoding="utf-8")
        barrier, winners = threading.Barrier(6), list[PipelineLock]()

        def contend(
            root: Path = root, barrier: threading.Barrier = barrier, winners: list[Any] = winners
        ) -> None:
            lock = PipelineLock(root)
            barrier.wait()
            try:
                lock.acquire()
                winners.append(lock)
            except LockHeldError:
                pass
            except Exception as exc:
                errors[type(exc).__name__] += 1

        ts = [threading.Thread(target=contend) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        doubles += len(winners) != 1
        assert winners[0].broken_stale == stale
        assert (
            json.loads((root / "pipeline.lock").read_text(encoding="utf-8"))["lock_id"]
            == winners[0].lock_id
        )
        for w in winners:
            w.release()
    assert (doubles, errors) == (0, Counter())


def test_force_release_on_a_missing_directory_is_harmless(tmp_path: Path) -> None:
    assert PipelineLock(tmp_path / "absent").force_release() is None
    assert not (tmp_path / "absent").exists()


def test_reading_the_owner_creates_nothing(tmp_path: Path) -> None:
    assert PipelineLock(tmp_path / "absent").owner() is None
    assert not (tmp_path / "absent").exists()


# ── 9. history integrity under concurrency ──


def test_hash_chain_stays_valid_with_concurrent_writer_processes(tmp_path: Path) -> None:
    procs = [spawn(CHILD_CHAIN, tmp_path, f"w{k}", 15) for k in range(4)]
    for p in procs:
        p.communicate(timeout=300)
        assert p.returncode == 0
    entries = HashChain(tmp_path / "history.jsonl").entries  # raises if the chain is broken
    assert [e["seq"] for e in entries] == list(range(1, 61))
    for k in range(4):
        assert [e["i"] for e in entries if e["writer"] == f"w{k}"] == list(range(15))
    assert not (tmp_path / "pipeline.lock").exists()


# ── R2: release must confirm the owner (transient / persistent read errors, invalid content) ──


def _flaky_reads(monkeypatch: pytest.MonkeyPatch, failures: int | None) -> Counter[str]:
    """Reading pipeline.lock fails with PermissionError `failures` times (None = always)."""
    real = Path.read_text
    calls: Counter[str] = Counter()

    def read_text(self: Path, *a: Any, **k: Any) -> str:
        if self.name == "pipeline.lock":
            calls["read"] += 1
            if failures is None or calls["read"] <= failures:
                raise PermissionError(13, "sharing violation")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    return calls


def test_transient_owner_read_error_is_retried_on_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = PipelineLock(tmp_path)
    lock.acquire()
    calls = _flaky_reads(monkeypatch, failures=3)
    lock.release()
    assert calls["read"] == 4  # three failures, then the owner was confirmed
    assert not lock.held and not lock.path.exists()


def test_persistent_owner_read_error_keeps_the_lock_and_allows_a_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lock_mod, "RETRY_TIMEOUT_S", 0.1)
    lock = PipelineLock(tmp_path)
    lock.acquire()
    _flaky_reads(monkeypatch, failures=None)
    with pytest.raises(LockReleaseError, match="owner could not be read"):
        lock.release()
    assert lock.held  # coherent: still ours, nothing deleted
    monkeypatch.undo()
    assert lock.path.exists()
    with pytest.raises(LockHeldError):
        PipelineLock(tmp_path).acquire()  # mutual exclusion kept while unreleased
    lock.release()  # later attempt succeeds
    assert not lock.held and not lock.path.exists()


def test_invalid_lock_content_is_never_deleted_by_release(tmp_path: Path) -> None:
    lock = PipelineLock(tmp_path)
    lock.acquire()
    lock.path.write_text("{corrupt", encoding="utf-8")  # replaced by someone else
    lock.release()
    assert not lock.held and lock.path.read_text(encoding="utf-8") == "{corrupt"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows exclusive share mode")
def test_exclusive_reader_only_delays_release(tmp_path: Path) -> None:
    """A real handle opened with share mode 0 (no reading, no deleting by others)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    lock = PipelineLock(tmp_path)
    lock.acquire()
    handle = kernel32.CreateFileW(str(lock.path), 0x80000000, 0, None, 3, 0x80, None)
    assert handle not in (None, wintypes.HANDLE(-1).value)
    threading.Timer(0.2, kernel32.CloseHandle, args=(handle,)).start()
    t0 = time.monotonic()
    lock.release()
    assert 0.1 < time.monotonic() - t0 < lock_mod.RETRY_TIMEOUT_S
    assert not lock.held and not lock.path.exists()


# ── B1: force_release() with the mutex busy raises the public error and changes nothing ──


def test_force_release_with_busy_mutex_raises_public_error_and_keeps_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lock_mod, "MUTEX_TIMEOUT_S", 0.2)
    holder = PipelineLock(tmp_path, purpose="test-holder")
    holder.acquire()
    before = holder.path.read_bytes()
    entered, leave = threading.Event(), threading.Event()

    def wedge() -> None:
        with lock_mod._os_mutex(holder.mutex_path, 5):
            entered.set()
            leave.wait(5)

    t = threading.Thread(target=wedge)
    t.start()
    entered.wait(5)
    try:
        with pytest.raises(LockReleaseError, match=r"mutex not acquired within 0.2 s") as info:
            PipelineLock(tmp_path).force_release()
        assert "force release not performed" in str(info.value)
        assert not isinstance(info.value, lock_mod._MutexTimeoutError)
        assert holder.path.read_bytes() == before and holder.mutex_path.exists()
    finally:
        leave.set()
        t.join()
    removed = PipelineLock(tmp_path).force_release()  # mutex free again: works as before
    assert removed is not None and removed["lock_id"] == holder.lock_id
    assert not holder.path.exists()
