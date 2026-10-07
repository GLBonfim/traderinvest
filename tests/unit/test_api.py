from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.api.main import app
from app.core.config import Settings, get_settings
from app.database.session import get_engine


def _settings() -> Settings:
    return Settings(_env_file=None, postgres_password="x")  # type: ignore[call-arg]


def test_health_reports_503_when_database_is_unreachable() -> None:
    # Port 1 on localhost refuses connections -> real OperationalError, no mocking of SQLAlchemy.
    dead = create_engine(
        "postgresql+psycopg://nobody:nopass@127.0.0.1:1/none", connect_args={"connect_timeout": 1}
    )
    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_engine] = lambda: dead
    try:
        resp = TestClient(app).get("/health")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["database"]["status"] == "unavailable"
    assert body["database"]["error"] == "OperationalError"
    assert body["trading_mode"] == "disabled"
    assert "nopass" not in resp.text
    assert resp.headers["x-request-id"]


def test_api_exposes_no_trading_or_order_endpoints() -> None:
    """Phase 1 must not expose any endpoint able to move money."""
    paths: dict[str, dict[str, object]] = app.openapi()["paths"]
    routes = {(method.upper(), path) for path, ops in paths.items() for method in ops}
    assert routes == {("GET", "/health")}
    forbidden = ("order", "trade", "broker", "execute", "position")
    assert not any(word in path.lower() for _, path in routes for word in forbidden)
