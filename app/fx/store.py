"""Local persistence for the FX engine (SQLite, one file per data directory).

- `journal`: append-only, hash-chained record of everything that happened (proposals, risk
  decisions, orders, fills, locks, commands). Each row's hash covers the previous hash, so any
  edit or deletion is detectable with `verify()`.
- `state`: named JSON documents (risk state, broker, market, orchestrator) written together in
  one transaction, so a restart always sees a consistent set.

Errors are classified (app/fx/errors.py): a file that is not a database ->
`StateUnreadableError`; an unsupported schema or a malformed document -> `StoreError`
(a `StateCorruptError`). The store never repairs, rewrites or deletes data by itself.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.fx.errors import StateCorruptError, StateUnreadableError

SCHEMA_VERSION = 1
GENESIS = "0" * 64


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


class StoreError(StateCorruptError):
    """Unsupported schema or malformed stored document."""


class FxStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)  # explicit transactions
        try:
            self._init_schema()
        except sqlite3.DatabaseError as exc:
            self.db.close()
            raise StateUnreadableError(
                f"a base de dados de estado {path.name} não pode ser lida: {type(exc).__name__}"
            ) from exc
        self._depth = 0

    def _init_schema(self) -> None:
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS journal (seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, "
            "kind TEXT NOT NULL, payload TEXT NOT NULL, prev_hash TEXT NOT NULL, "
            "hash TEXT NOT NULL)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS journal_kind ON journal (kind, seq)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at TEXT NOT NULL)"
        )
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        elif int(row[0]) != SCHEMA_VERSION:
            raise StoreError(f"unsupported store schema {row[0]} (expected {SCHEMA_VERSION})")

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")
        finally:
            self._depth = 0

    # --- journal ---------------------------------------------------------------------------

    def _last(self) -> tuple[int, str]:
        row = self.db.execute("SELECT seq, hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        return (0, GENESIS) if row is None else (int(row[0]), str(row[1]))

    def append(self, kind: str, payload: Mapping[str, Any], ts: datetime | None = None) -> int:
        stamp = (ts or datetime.now(UTC)).isoformat()
        with self.transaction():
            seq, prev = self._last()
            seq += 1
            body = _canon(payload)
            digest = hashlib.sha256(_canon([seq, stamp, kind, body, prev]).encode()).hexdigest()
            self.db.execute(
                "INSERT INTO journal VALUES (?, ?, ?, ?, ?, ?)",
                (seq, stamp, kind, body, prev, digest),
            )
        return seq

    def recent(self, limit: int = 100, kinds: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        if kinds:
            marks = ",".join("?" * len(kinds))
            rows = self.db.execute(
                f"SELECT seq, ts, kind, payload FROM journal WHERE kind IN ({marks}) "  # noqa: S608
                "ORDER BY seq DESC LIMIT ?",
                (*kinds, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT seq, ts, kind, payload FROM journal ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"seq": s, "ts": t, "kind": k, "payload": json.loads(p)}
            for s, t, k, p in reversed(rows)
        ]

    def verify(self) -> tuple[bool, int | None]:
        """(True, None) when the chain is intact, else (False, first bad seq)."""
        prev = GENESIS
        expected = 1
        for seq, ts, kind, body, p, h in self.db.execute(
            "SELECT seq, ts, kind, payload, prev_hash, hash FROM journal ORDER BY seq"
        ):
            calc = hashlib.sha256(_canon([seq, ts, kind, body, prev]).encode()).hexdigest()
            if seq != expected or p != prev or h != calc:
                return False, int(seq)
            prev, expected = h, expected + 1
        return True, None

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM journal").fetchone()[0])

    def head(self) -> tuple[int, str]:
        """(last seq, last hash); (0, GENESIS) for an empty journal."""
        return self._last()

    def hash_at(self, seq: int) -> str | None:
        row = self.db.execute("SELECT hash FROM journal WHERE seq=?", (seq,)).fetchone()
        return None if row is None else str(row[0])

    def last_kind(self) -> str | None:
        row = self.db.execute("SELECT kind FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
        return None if row is None else str(row[0])

    # --- state -----------------------------------------------------------------------------

    def put_state(self, key: str, value: Mapping[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO state VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "value=excluded.value, updated_at=excluded.updated_at",
            (key, _canon(value), datetime.now(UTC).isoformat()),
        )

    def put_states(self, items: Mapping[str, Mapping[str, Any]]) -> None:
        with self.transaction():
            for k, v in items.items():
                self.put_state(k, v)

    def get_state(self, key: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise StoreError(f"state {key!r} is not valid JSON") from exc
        if not isinstance(value, dict):
            raise StoreError(f"state {key!r} is not an object")
        return value

    def clear_state(self) -> None:
        with self.transaction():
            self.db.execute("DELETE FROM state")
