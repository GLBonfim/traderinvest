"""Verified read-only database access for research loaders (risk research CLI).

`PGOPTIONS` cannot be used: the application passes its own libpq `options` parameter, which
takes precedence over the environment variable. Instead, protection is applied per connection
and verified per statement:

1. `read_only_engine()` is a dedicated, short-lived engine (NullPool: every connection is new
   and closed after use, no pre-ping, nothing returns to the application's pool) whose
   connections carry the SQLAlchemy execution option `postgresql_readonly=True`. With psycopg 3
   this sets `Connection.read_only = True` on the DBAPI connection when the SQLAlchemy
   connection is created, so psycopg starts EVERY transaction on it with `BEGIN READ ONLY`
   (native PostgreSQL protection; writes fail in the server) — including transactions started
   after a commit or rollback, and on any new connection of this engine.
2. `ReadOnlyGuard` runs before every statement sent through the engine. It refuses anything
   that is not SELECT or SHOW, refuses a DBAPI connection whose `read_only` is not True, and —
   on the same cursor, in the same transaction, immediately before the statement — runs
   `SHOW transaction_read_only` and refuses unless the server answers `on`. The first statement
   of a transaction therefore opens it (`BEGIN READ ONLY`) and verifies it before any data is
   read; no statement is ever sent without that confirmation.
3. `read_only_session()` yields a Session bound to one such connection after an explicit
   `SHOW transaction_read_only` check, and records the evidence (`ReadOnlyEvidence`). Any
   failed check raises `ReadOnlyViolationError` before the statement is sent.
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.database.session import connect_args

READ_ONLY_OPTION = "postgresql_readonly"
MECHANISM = (
    "dedicated NullPool engine with SQLAlchemy execution option postgresql_readonly=True "
    "(psycopg Connection.read_only=True: every transaction starts with BEGIN READ ONLY); "
    "before every statement, on the same cursor and transaction: only SELECT/SHOW allowed, "
    "DBAPI read_only must be True and SHOW transaction_read_only must return 'on'"
)
_ALLOWED = re.compile(r"^\s*(SELECT|SHOW)\b", re.IGNORECASE)


class ReadOnlyViolationError(RuntimeError):
    """Read-only access could not be confirmed; the statement was not sent."""


@dataclass
class ReadOnlyEvidence:
    mechanism: str = MECHANISM
    statements: int = 0  # statements sent after a successful check
    checks: int = 0  # SHOW transaction_read_only checks performed
    values: list[str] = field(default_factory=list)  # distinct answers seen, in order
    violation: str | None = None

    @property
    def confirmed(self) -> bool:
        """True only if at least one statement ran and every one was preceded by 'on'."""
        return (
            self.violation is None
            and self.statements > 0
            and self.checks == self.statements
            and self.values == ["on"]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mechanism": self.mechanism,
            "read_only_confirmed": self.confirmed,
            "statements_checked": self.statements,
            "checks": self.checks,
            "transaction_read_only_values": list(self.values),
            "violation": self.violation,
        }


class ReadOnlyGuard:
    """`before_cursor_execute` listener; see the module docstring."""

    def __init__(self, evidence: ReadOnlyEvidence) -> None:
        self.evidence = evidence

    def _refuse(self, reason: str) -> None:
        self.evidence.violation = reason
        raise ReadOnlyViolationError(reason)

    def __call__(
        self,
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        ev = self.evidence
        if not _ALLOWED.match(statement or ""):
            self._refuse(f"statement refused (only SELECT/SHOW allowed): {statement[:40]!r}")
        if getattr(cursor.connection, "read_only", None) is not True:
            self._refuse("DBAPI connection is not in read-only mode")
        try:
            cursor.execute("SHOW transaction_read_only")
            row = cursor.fetchone()
        except Exception as exc:  # the check itself failed: never send the statement
            self._refuse(f"transaction_read_only check failed: {type(exc).__name__}")
        value = None if row is None else row[0]
        ev.checks += 1
        if str(value) not in ev.values:
            ev.values.append(str(value))
        if value != "on":
            self._refuse(f"transaction_read_only is {value!r}, not 'on'")
        ev.statements += 1


def read_only_engine() -> Engine:
    """Dedicated engine for one research load; the caller disposes of it."""
    settings = get_settings()
    return create_engine(
        settings.database_url(),
        poolclass=NullPool,
        connect_args=connect_args(settings),
        execution_options={READ_ONLY_OPTION: True},
    )


@contextmanager
def read_only_session(
    engine: Engine, evidence: ReadOnlyEvidence | None = None
) -> Iterator[tuple[Session, ReadOnlyEvidence]]:
    """A Session on one verified read-only connection of `engine` (see the module docstring).
    The guard stays installed on the engine for the whole block, so every statement, every
    transaction and any additional connection opened through the engine is checked."""
    if engine.get_execution_options().get(READ_ONLY_OPTION) is not True:
        raise ReadOnlyViolationError(f"engine does not set {READ_ONLY_OPTION}=True")
    evidence = evidence if evidence is not None else ReadOnlyEvidence()
    guard = ReadOnlyGuard(evidence)
    event.listen(engine, "before_cursor_execute", guard)
    try:
        with engine.connect() as conn:
            if conn.get_execution_options().get(READ_ONLY_OPTION) is not True:
                raise ReadOnlyViolationError(f"connection does not carry {READ_ONLY_OPTION}=True")
            if getattr(conn.connection.dbapi_connection, "read_only", None) is not True:
                raise ReadOnlyViolationError("DBAPI connection is not in read-only mode")
            with Session(bind=conn, autoflush=False) as session:
                value = session.execute(text("SHOW transaction_read_only")).scalar()
                if value != "on":  # unreachable unless the guard is bypassed: double check
                    raise ReadOnlyViolationError(f"transaction_read_only is {value!r}, not 'on'")
                yield session, evidence
                session.rollback()  # nothing to keep: end the read-only transaction
    finally:
        event.remove(engine, "before_cursor_execute", guard)
