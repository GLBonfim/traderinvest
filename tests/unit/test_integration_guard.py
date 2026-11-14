"""Phase 6 guard of the destructive integration fixture: DROP/CREATE DATABASE only on the
explicitly marked disposable server, checked before any destructive SQL. Fake engines only:
no connection to any real database is made."""

from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from tests.integration import conftest as it

MARKER = "it-guard-0123456789abcdef"
GOOD = {"APP_ENV": "test", it.TARGET_ENV: "127.0.0.1:55432", it.MARKER_ENV: MARKER}


class FakeConn:
    def __init__(self, engine: "FakeEngine") -> None:
        self.engine = engine

    def __enter__(self) -> "FakeConn":
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def execute(self, stmt: Any) -> Any:
        sql = str(stmt)
        self.engine.sql.append(sql)
        if "shobj_description" in sql:
            if isinstance(self.engine.marker, BaseException):
                raise self.engine.marker
            return type("R", (), {"scalar": lambda _s: self.engine.marker})()
        return None


class FakeEngine:
    def __init__(self, marker: Any = MARKER) -> None:
        self.marker = marker
        self.sql: list[str] = []

    def connect(self) -> FakeConn:
        return FakeConn(self)

    def destructive(self) -> list[str]:
        return [s for s in self.sql if s.startswith(("DROP", "CREATE"))]


def settings(host: str = "127.0.0.1", port: int = 55432) -> Settings:
    return Settings(_env_file=None, postgres_password="x", postgres_host=host, postgres_port=port)


def test_marked_disposable_server_is_accepted_and_checked_before_drop() -> None:
    eng = FakeEngine()
    it.recreate_test_database(GOOD, settings(), eng, "quant_test")
    assert "shobj_description" in eng.sql[0]  # the read-only check comes first
    assert eng.sql[1:] == [
        'DROP DATABASE IF EXISTS "quant_test" WITH (FORCE)',
        'CREATE DATABASE "quant_test"',
    ]


@pytest.mark.parametrize(
    "env",
    [
        {},
        {**GOOD, "APP_ENV": "development"},
        {k: v for k, v in GOOD.items() if k != "APP_ENV"},
        {k: v for k, v in GOOD.items() if k != it.TARGET_ENV},
        {k: v for k, v in GOOD.items() if k != it.MARKER_ENV},
        {**GOOD, it.MARKER_ENV: "   "},
    ],
)
def test_not_configured_runs_no_sql_at_all(env: dict[str, str]) -> None:
    eng = FakeEngine()
    with pytest.raises(it.TargetNotConfiguredError):
        it.recreate_test_database(env, settings(), eng, "quant_test")
    assert eng.sql == []  # not even the read-only check


@pytest.mark.parametrize(
    ("env", "cfg", "marker", "message"),
    [
        (GOOD, settings(port=5432), MARKER, "does not match the configured server"),  # dev port
        (GOOD, settings(host="localhost"), MARKER, "does not match the configured server"),
        ({**GOOD, it.TARGET_ENV: "127.0.0.1:5432"}, settings(), MARKER, "does not match"),
        ({**GOOD, it.MARKER_ENV: "short"}, settings(), "short", "at least 16"),
    ],
)
def test_target_mismatch_is_refused_before_any_query(
    env: dict[str, str], cfg: Settings, marker: str, message: str
) -> None:
    eng = FakeEngine(marker)
    with pytest.raises(it.UnsafeTestTargetError, match=message):
        it.recreate_test_database(env, cfg, eng, "quant_test")
    assert eng.sql == []


@pytest.mark.parametrize(
    "server_marker",
    [None, "", "it-guard-ffffffffffffffff", MARKER + " ", RuntimeError("permission denied")],
)
def test_unmarked_or_wrongly_marked_server_is_refused_before_drop(server_marker: Any) -> None:
    """E.g. the development server: same credentials could connect, but it carries no marker."""
    eng = FakeEngine(server_marker)
    with pytest.raises(it.UnsafeTestTargetError):
        it.recreate_test_database(GOOD, settings(), eng, "quant_test")
    assert eng.destructive() == [] and len(eng.sql) == 1  # only the read-only check ran


def test_unreachable_server_is_not_disguised_as_unsafe() -> None:
    eng = FakeEngine(OperationalError("select", {}, Exception("connection refused")))
    with pytest.raises(OperationalError):  # the fixture turns this into a skip
        it.recreate_test_database(GOOD, settings(), eng, "quant_test")
    assert eng.destructive() == []


def test_name_alone_proves_nothing() -> None:
    eng = FakeEngine(marker=None)
    with pytest.raises(it.UnsafeTestTargetError):
        it.recreate_test_database(GOOD, settings(), eng, "quant_test")
    assert eng.destructive() == []
