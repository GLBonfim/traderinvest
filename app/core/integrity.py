"""Shared integrity primitives: canonical JSON, SHA-256 entry hashes, atomic file replacement and
an append-only hash-chained JSONL file.

Moved unchanged from `app.alerts.store` (where the alert, broker and operations stores import
them) so that modules outside the alerts package — e.g. the research run registry, which a
read-only API may load — can use them without importing `app.alerts`. The algorithms and the
on-disk format are identical; `app.alerts.store` keeps its old names as aliases.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any, ClassVar


class ChainError(RuntimeError):
    """A hash-chained file is corrupt or inconsistent; nothing was modified."""


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def entry_hash(entry: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json({k: v for k, v in entry.items() if k != "hash"}).encode()
    ).hexdigest()


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class HashChain:
    """Append-only JSONL file with a SHA-256 chain (seq, prev_hash, hash).

    `error` is the exception raised for a corrupt file (subclasses set their own type)."""

    error: ClassVar[type[Exception]] = ChainError

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[dict[str, Any]] = []
        if path.exists():
            prev = ""
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, start=1):
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise self.error(f"{path.name} line {n} is not valid JSON") from exc
                    if (
                        e.get("seq") != n
                        or e.get("prev_hash") != prev
                        or e.get("hash") != entry_hash(e)
                    ):
                        raise self.error(f"{path.name} line {n}: hash chain broken")
                    prev = e["hash"]
                    self.entries.append(e)

    def append(self, bodies: list[dict[str, Any]]) -> None:
        if not bodies:
            return
        prev = self.entries[-1]["hash"] if self.entries else ""
        new: list[dict[str, Any]] = []
        for b in bodies:
            e = {"seq": len(self.entries) + len(new) + 1, **b, "prev_hash": prev}
            e["hash"] = entry_hash(e)
            new.append(e)
            prev = e["hash"]
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("".join(canonical_json(e) + "\n" for e in new))
            fh.flush()
            os.fsync(fh.fileno())
        self.entries += new
