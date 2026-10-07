"""Engine and session factory (SQLAlchemy 2.x, sync, psycopg 3)."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url(),
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": settings.db_connect_timeout_s,
            # All timestamps are stored and compared in UTC.
            "options": "-c timezone=UTC",
        },
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with get_session_factory()() as session:
        yield session
