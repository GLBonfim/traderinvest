"""Integration fixtures: a throwaway `<POSTGRES_DB>_test` database, migrated to head.

The `test_db_url` fixture DROPS and CREATES a database, so it runs only against a server that is
explicitly and verifiably a disposable test server (Phase 6 guard):

1. `APP_ENV=test`;
2. `TRADERINVEST_TEST_DB_TARGET=<host>:<port>` names the test server and must equal the host
   and port of the application settings (configured independently of the credentials);
3. `TRADERINVEST_TEST_DB_MARKER=<random token, >= 16 chars>` must equal the comment of the
   `postgres` database on that server. Whoever provisions the disposable server writes it
   (`COMMENT ON DATABASE postgres IS '<token>'`); the development server does not carry it.

The target is checked with a read-only query BEFORE any DROP/CREATE. Without this
configuration the destructive fixtures are skipped without executing any SQL; a configuration
that does not match the server it points to fails the test run. The name `quant_test` alone
proves nothing and is never used as evidence. The development database is never modified.
"""

from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
TARGET_ENV = "TRADERINVEST_TEST_DB_TARGET"
MARKER_ENV = "TRADERINVEST_TEST_DB_MARKER"
MIN_MARKER_LENGTH = 16
MARKER_SQL = (
    "select shobj_description(oid, 'pg_database') from pg_database where datname = 'postgres'"
)

pytestmark = pytest.mark.integration


class TargetNotConfiguredError(RuntimeError):
    """The destructive fixtures are not enabled (no SQL is executed)."""


class UnsafeTestTargetError(RuntimeError):
    """A destructive fixture was configured for a server that is not the disposable one."""


def check_destructive_target(
    env: Mapping[str, str], host: str, port: int, read_marker: Callable[[], str | None]
) -> None:
    """Raises unless the configured server is the explicitly marked disposable test server.
    `read_marker` performs the only (read-only) query and is called last."""
    if env.get("APP_ENV") != "test":
        raise TargetNotConfiguredError("APP_ENV is not 'test'")
    target, marker = env.get(TARGET_ENV, "").strip(), env.get(MARKER_ENV, "").strip()
    if not target or not marker:
        raise TargetNotConfiguredError(f"{TARGET_ENV} and {MARKER_ENV} must both be set")
    if target != f"{host}:{port}":
        raise UnsafeTestTargetError(
            f"{TARGET_ENV}={target!r} does not match the configured server {host}:{port}"
        )
    if len(marker) < MIN_MARKER_LENGTH:
        raise UnsafeTestTargetError(f"{MARKER_ENV} must have at least {MIN_MARKER_LENGTH} chars")
    try:
        found = read_marker()
    except OperationalError:  # server not reachable: nothing was executed (callers skip)
        raise
    except Exception as exc:  # cannot be verified -> refuse
        raise UnsafeTestTargetError(f"server marker unreadable ({type(exc).__name__})") from exc
    if found != marker:
        raise UnsafeTestTargetError("server marker does not match: not the disposable test server")


def recreate_test_database(
    env: Mapping[str, str], settings: Settings, admin: Any, name: str
) -> None:
    """Guard first (one read-only query), then DROP/CREATE through the same admin engine."""

    def read_marker() -> str | None:
        with admin.connect() as conn:
            value = conn.execute(text(MARKER_SQL)).scalar()
        return None if value is None else str(value)

    check_destructive_target(env, settings.postgres_host, settings.postgres_port, read_marker)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))


@pytest.fixture(scope="session")
def settings() -> Settings:
    try:
        return Settings()
    except Exception as exc:  # missing .env / POSTGRES_PASSWORD
        pytest.skip(f"Database settings unavailable: {exc}")


@pytest.fixture(scope="session")
def test_db_url(settings: Settings) -> Iterator[URL]:
    import os

    name = f"{settings.postgres_db}_test"
    admin = create_engine(
        settings.database_url("postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 3},
    )
    try:
        recreate_test_database(os.environ, settings, admin, name)
    except TargetNotConfiguredError as exc:
        admin.dispose()
        pytest.skip(f"destructive integration fixture disabled: {exc}")
    except UnsafeTestTargetError as exc:
        admin.dispose()
        pytest.fail(f"refusing to DROP/CREATE {name}: {exc}", pytrace=False)
    except OperationalError:
        admin.dispose()
        pytest.skip("PostgreSQL not reachable (run: docker compose up -d db)")

    yield settings.database_url(name)

    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def alembic_config(url: URL) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = url.render_as_string(hide_password=False)
    return cfg


@pytest.fixture(scope="session")
def migrated_engine(test_db_url: URL) -> Iterator[Engine]:
    command.upgrade(alembic_config(test_db_url), "head")
    engine = create_engine(test_db_url, connect_args={"options": "-c timezone=UTC"})
    yield engine
    engine.dispose()
