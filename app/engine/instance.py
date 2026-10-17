"""Single engine per data directory: an exclusive OS lock on `<data>/engine.lock`, held for the
engine's lifetime and released by the OS if the process dies (no stale lock files)."""

import os
import sys
from pathlib import Path

if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


class EngineAlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, root: Path) -> None:
        self.path = root / "engine.lock"
        self.fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            _lock(fd)
        except OSError as exc:
            os.close(fd)
            raise EngineAlreadyRunningError(
                f"another engine is already using {self.path.parent}"
            ) from exc
        self.fd = fd

    def release(self) -> None:
        if self.fd is not None:
            try:
                _unlock(self.fd)
            finally:
                os.close(self.fd)
                self.fd = None
