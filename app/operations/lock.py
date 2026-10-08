"""Single-run lock: one pipeline at a time per operations directory.

Acquisition creates `pipeline.lock` with O_CREAT | O_EXCL (atomic on NTFS and POSIX file
systems) and writes the owner: lock_id, host, pid, acquired_at, purpose. Release removes the file
only if it still carries our lock_id.

Stale locks: a lock is broken automatically ONLY when it was taken on this host and its process
is verified dead. A lock from another host, or whose process is alive (possibly a reused PID), is
never removed automatically; `force_release()` exists for an operator who has verified ownership.
`os.kill(pid, 0)` is NOT used on Windows (there it terminates the process); liveness is checked
with OpenProcess / GetExitCodeProcess.
"""

import json
import os
import socket
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STILL_ACTIVE = 259


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


@dataclass
class PipelineLock:
    root: Path
    purpose: str = "pipeline"

    def __post_init__(self) -> None:
        self.path = Path(self.root) / "pipeline.lock"
        self.lock_id = uuid.uuid4().hex
        self.held = False
        self.broken_stale: dict[str, Any] | None = None

    def owner(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"unreadable": True}
        return data if isinstance(data, dict) else {"unreadable": True}

    def is_stale(self, owner: dict[str, Any]) -> bool:
        return (
            owner.get("host") == socket.gethostname()
            and isinstance(owner.get("pid"), int)
            and not pid_alive(int(owner["pid"]))
        )

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                owner = self.owner()
                if owner is None:
                    continue  # released in between
                if self.is_stale(owner):
                    self.broken_stale = owner
                    self.path.unlink(missing_ok=True)
                    continue
                raise LockHeldError(owner) from None
            info = {
                "lock_id": self.lock_id,
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "acquired_at": datetime.now(UTC).isoformat(),
                "purpose": self.purpose,
            }
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(info))
                fh.flush()
                os.fsync(fh.fileno())
            self.held = True
            return
        raise LockHeldError(self.owner() or {})

    def release(self) -> None:
        if not self.held:
            return
        owner = self.owner()
        if owner and owner.get("lock_id") == self.lock_id:
            self.path.unlink(missing_ok=True)
        self.held = False

    def force_release(self) -> dict[str, Any] | None:
        """Operator action after verifying the owner is gone. Returns the removed owner."""
        owner = self.owner()
        self.path.unlink(missing_ok=True)
        return owner

    def __enter__(self) -> "PipelineLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
