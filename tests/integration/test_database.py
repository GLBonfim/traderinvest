from datetime import UTC, datetime
from decimal import Decimal

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import URL, Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.main import app
from app.core.config import Settings, get_settings
from app.database.models import Instrument, PriceBar
from app.database.session import get_engine
from tests.integration.conftest import alembic_config

pytestmark = pytest.mark.integration

EXPECTED_TABLES = {
    "alembic_version",
    "instruments",
    "provider_symbols",
    "price_bars",
    "market_sessions",
    "data_quality_events",
}


def _head(url: URL) -> str:
    head = ScriptDirectory.from_config(alembic_config(url)).get_current_head()
    assert head is not None
    return head


def test_migrations_create_expected_tables(migrated_engine: Engine) -> None:
    assert set(inspect(migrated_engine).get_table_names()) == EXPECTED_TABLES


def test_models_match_migrations(test_db_url: URL, migrated_engine: Engine) -> None:
    """Fails if someone changes a model without writing a migration."""
    command.check(alembic_config(test_db_url))


def test_migrations_downgrade_and_upgrade_cleanly(
    test_db_url: URL, migrated_engine: Engine
) -> None:
    cfg = alembic_config(test_db_url)
    command.downgrade(cfg, "base")
    assert set(inspect(migrated_engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(cfg, "head")
    assert set(inspect(migrated_engine).get_table_names()) == EXPECTED_TABLES


def test_session_timezone_is_utc(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        assert conn.execute(text("SHOW timezone")).scalar_one() == "UTC"


def _instrument(session: Session) -> Instrument:
    spy = Instrument(
        underlying="S&P 500",
        symbol="SPY",
        name="SPDR S&P 500 ETF Trust",
        asset_class="etf",
        exchange="ARCX",
        currency="USD",
        timezone="America/New_York",
    )
    session.add(spy)
    session.flush()
    return spy


def _bar(instrument_id: int, timeframe: str = "1d") -> PriceBar:
    # Synthetic structural fixture only — not market data.
    return PriceBar(
        instrument_id=instrument_id,
        provider="test",
        timeframe=timeframe,
        ts=datetime(2000, 1, 3, 14, 30, tzinfo=UTC),
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=Decimal("0"),
        is_closed=True,
    )


def test_duplicate_bars_are_rejected(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as session:
        spy = _instrument(session)
        session.add(_bar(spy.id))
        session.flush()
        session.add(_bar(spy.id))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_unknown_timeframe_is_rejected(migrated_engine: Engine) -> None:
    with Session(migrated_engine) as session:
        spy = _instrument(session)
        session.add(_bar(spy.id, timeframe="2h"))
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


def test_health_is_ok_against_real_database(
    settings: Settings, test_db_url: URL, migrated_engine: Engine
) -> None:
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_engine] = lambda: migrated_engine
    try:
        resp = TestClient(app).get("/health")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == {
        "status": "ok",
        "schema_revision": _head(test_db_url),
        "error": None,
    }
    assert body["trading_mode"] == "disabled"
