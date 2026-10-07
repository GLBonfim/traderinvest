"""Integration fixtures: a throwaway `<POSTGRES_DB>_test` database, migrated to head.

Tests are skipped (not failed) when PostgreSQL is not reachable, so the unit suite
can run anywhere. The development database is never modified.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.exc import OperationalError

from app.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def settings() -> Settings:
    try:
        return Settings()  # type: ignore[call-arg]
    except Exception as exc:  # missing .env / POSTGRES_PASSWORD
        pytest.skip(f"Database settings unavailable: {exc}")


@pytest.fixture(scope="session")
def test_db_url(settings: Settings) -> Iterator[URL]:
    name = f"{settings.postgres_db}_test"
    admin = create_engine(
        settings.database_url("postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 3},
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError:
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
