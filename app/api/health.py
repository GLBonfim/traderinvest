"""Health check. Reports real database connectivity; never fakes an OK."""

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from app import __version__
from app.core.config import Settings, TradingMode, get_settings
from app.core.logging import get_logger
from app.database.session import get_engine

router = APIRouter(tags=["health"])
log = get_logger(__name__)


class DatabaseHealth(BaseModel):
    status: Literal["ok", "unavailable"]
    schema_revision: str | None = None
    error: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    trading_mode: TradingMode
    timestamp: datetime
    database: DatabaseHealth


def check_database(engine: Engine) -> DatabaseHealth:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            has_alembic = conn.execute(
                text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
            ).scalar_one()
            revision = (
                conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
                if has_alembic
                else None
            )
        return DatabaseHealth(status="ok", schema_revision=revision)
    except SQLAlchemyError as exc:
        # Only the exception class is exposed: driver messages may contain host/user details.
        log.error("health.database_unavailable", error_type=type(exc).__name__)
        return DatabaseHealth(status="unavailable", error=type(exc).__name__)


@router.get("/health", response_model=HealthResponse)
def health(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> HealthResponse:
    db = check_database(engine)
    healthy = db.status == "ok"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if healthy else "degraded",
        version=__version__,
        environment=settings.app_env,
        trading_mode=settings.trading_mode,
        timestamp=datetime.now(UTC),
        database=db,
    )
