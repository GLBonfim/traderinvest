"""Single-run lock: one pipeline at a time per operations directory.

Acquisition creates `pipeline.lock` with O_CREAT | O_EXCL (atomic on NTFS and POSIX file
systems) and writes the owner: lock_id, host, pid, acquired_at, purpose. Release removes the file
only if it still carries our lock_id.

Stale locks: a lock is broken automatically ONLY when it was taken on this host and its process
is verified dead. A lock from another host, or whose process is alive (possibly a reused PID), is
never removed automatically; `force_release()` exists for an operator who has verified ownership.
`os.kill(pid, 0)` is NOT used on Windows (there it terminates the process); liveness is checked
with OpenProcess / GetExitCodeProcess.

Concurrency (Phase 5E). On Windows a file cannot be deleted while another handle has it open,
so a contender reading the owner made `release()` fail with PermissionError and left a lock
behind that no one could break (its process was alive). Acquire, stale-break, release and
force-release therefore run their short check-then-act sections under an OS mutex: an exclusive
`msvcrt.locking` / `fcntl.flock` lock on the persistent `pipeline.lock.mutex` file (never
deleted; the OS drops it if the process dies). Only one contender at a time can then inspect,
break, create or remove `pipeline.lock`, so a stale lock is never broken twice and a fresh lock
is never removed by a contender that read an older owner. Transient sharing errors from readers
that do not take the mutex (`owner()`) are retried within RETRY_TIMEOUT_S; a release that still
cannot read the owner or remove the file raises LockReleaseError and the lock stays held (release
can be retried). A lock file whose owner cannot be confirmed is never deleted by `release()`.
"""

import errno
import json
import os
import socket
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STILL_ACTIVE = 259
MUTEX_TIMEOUT_S = 10.0  # the guarded sections take milliseconds; this bounds a wedged peer
RETRY_TIMEOUT_S = 2.0  # transient sharing violations from concurrent readers
RETRY_SLEEP_S = 0.005
_BUSY_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EWOULDBLOCK}


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: exists, owned by someone else
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # cannot tell: assume alive (never break a possibly-held lock)
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX: signal 0 only checks existence/permission
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LockHeldError(RuntimeError):
    def __init__(self, owner: dict[str, Any]) -> None:
        super().__init__(
            f"pipeline lock held by {owner.get('host')}:{owner.get('pid')} "
            f"since {owner.get('acquired_at')}"
        )
        self.owner = owner


class LockReleaseError(PermissionError):
    """The lock file could not be removed within the retry bound (e.g. another program keeps
    it open). The lock is still held by this instance; `release()` may be called again."""


class _MutexTimeoutError(Exception):
    pass


if sys.platform == "win32":
    import msvcrt

    def _lock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock_fd(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_fd(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def _os_mutex(path: Path, timeout: float) -> Iterator[None]:
    """Exclusive OS lock on `path` (created if needed, never deleted); threads and processes."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _lock_fd(fd)
                break
            except OSError as exc:
                if exc.errno not in _BUSY_ERRNOS:
                    raise
                if time.monotonic() >= deadline:
                    raise _MutexTimeoutError(str(path)) from None
                time.sleep(RETRY_SLEEP_S)
        try:
            yield
        finally:
            _unlock_fd(fd)
    finally:
        os.close(fd)


def _retry_sharing[T](action: Callable[[], T]) -> T:
    """Retries `action` while it fails with PermissionError (Windows sharing violation or
    delete pending caused by a concurrent reader); re-raises the last error after
    RETRY_TIMEOUT_S."""
    deadline = time.monotonic() + RETRY_TIMEOUT_S
    while True:
        try:
            return action()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(RETRY_SLEEP_S)


@dataclass
class PipelineLock:
    root: Path
    purpose: str = "pipeline"

    def __post_init__(self) -> None:
        self.path = Path(self.root) / "pipeline.lock"
        self.mutex_path = Path(self.root) / "pipeline.lock.mutex"
        self.lock_id = uuid.uuid4().hex
        self.held = False
        self.broken_stale: dict[str, Any] | None = None

    def owner(self) -> dict[str, Any] | None:
        return self._read_owner()[0]

    def _read_owner(self) -> tuple[dict[str, Any] | None, OSError | None]:
        """(owner, read error). A read error (e.g. a sharing violation) may be transient; invalid
        content is not an error here: it is reported as unreadable and is never ours (our own
        lock file is always written completely, under the mutex, before anyone can read it)."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, None
        except OSError as exc:
            return {"unreadable": True}, exc
        try:
            data = json.loads(text)
        except ValueError:
            return {"unreadable": True}, None
        return (data if isinstance(data, dict) else {"unreadable": True}), None

    def _confirmed_owner(self) -> dict[str, Any] | None:
        """Owner for release: transient read errors are retried within RETRY_TIMEOUT_S; if the
        owner still cannot be read, LockReleaseError (never delete an unconfirmed lock)."""
        deadline = time.monotonic() + RETRY_TIMEOUT_S
        while True:
            owner, error = self._read_owner()
            if error is None:
                return owner
            if time.monotonic() >= deadline:
                raise LockReleaseError(
                    error.errno or errno.EIO,
                    f"pipeline lock owner could not be read; lock not released: {self.path}",
                ) from error
            time.sleep(RETRY_SLEEP_S)

    def is_stale(self, owner: dict[str, Any]) -> bool:
        return (
            owner.get("host") == socket.gethostname()
            and isinstance(owner.get("pid"), int)
            and not pid_alive(int(owner["pid"]))
        )

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with _os_mutex(self.mutex_path, MUTEX_TIMEOUT_S):
                self._acquire_guarded()
        except _MutexTimeoutError:
            raise LockHeldError(self.owner() or {"unreadable": True}) from None

    def _acquire_guarded(self) -> None:
        for _ in range(2):
            try:
                fd = _retry_sharing(
                    lambda: os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                )
            except FileExistsError:
                owner = self.owner()
                if owner is None:
                    continue  # removed in between (e.g. by an operator)
                if self.is_stale(owner):
                    self.broken_stale = owner
                    _retry_sharing(lambda: self.path.unlink(missing_ok=True))
                    continue
                raise LockHeldError(owner) from None
            info = {
                "lock_id": self.lock_id,
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "acquired_at": datetime.now(UTC).isoformat(),
                "purpose": self.purpose,
            }
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(info))
                    fh.flush()
                    os.fsync(fh.fileno())
            except BaseException:
                # never leave an empty, unbreakable lock file behind
                _retry_sharing(lambda: self.path.unlink(missing_ok=True))
                raise
            self.held = True
            return
        raise LockHeldError(self.owner() or {})

    def release(self) -> None:
        if not self.held:
            return
        try:
            with _os_mutex(self.mutex_path, MUTEX_TIMEOUT_S):
                owner = self._confirmed_owner()
                if owner and owner.get("lock_id") == self.lock_id:
                    try:
                        _retry_sharing(lambda: self.path.unlink(missing_ok=True))
                    except PermissionError as exc:
                        raise LockReleaseError(
                            exc.errno, f"pipeline lock could not be removed: {self.path}"
                        ) from exc
        except _MutexTimeoutError:
            raise LockReleaseError(
                errno.EBUSY, f"pipeline lock mutex busy; lock not released: {self.path}"
            ) from None
        self.held = False

    def force_release(self) -> dict[str, Any] | None:
        """Operator action after verifying the owner is gone. Returns the removed owner."""
        if not self.path.parent.is_dir():
            return None
        with _os_mutex(self.mutex_path, MUTEX_TIMEOUT_S):
            owner = self.owner()
            _retry_sharing(lambda: self.path.unlink(missing_ok=True))
        return owner

    def __enter__(self) -> "PipelineLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
