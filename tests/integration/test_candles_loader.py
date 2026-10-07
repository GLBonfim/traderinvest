from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.candles.engine import CandlestickEngine
from app.candles.loader import load_closed_bars
from app.data.calendar import TradingCalendar
from app.data.ingestion import ingest_daily_bars
from app.data.instruments import SPY
from tests.factories import StubProvider, regular_rows

pytestmark = pytest.mark.integration


@pytest.fixture
def session(migrated_engine: Engine) -> Iterator[Session]:
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE data_quality_events, ingestion_runs, price_bars, market_sessions, "
                "provider_symbols, instruments RESTART IDENTITY CASCADE"
            )
        )
    with Session(migrated_engine) as s:
        yield s


def test_loader_returns_only_closed_raw_bars_in_utc(session: Session) -> None:
    during_last_session = datetime(2024, 7, 9, 15, 0, tzinfo=UTC)
    ingest_daily_bars(
        session,
        StubProvider(regular_rows()),
        SPY,
        date(2024, 7, 1),
        date(2024, 7, 9),
        calendar=TradingCalendar("XNYS"),
        as_of=during_last_session,
    )

    instrument_id, bars = load_closed_bars(session, "SPY")

    assert len(bars) == 5  # the forming 07-09 bar is excluded
    assert str(bars.index.tz) == "UTC"
    assert bars.index.is_monotonic_increasing
    assert bars.index[0] == datetime(2024, 7, 1, 13, 30, tzinfo=UTC)
    assert list(bars.columns) == ["open", "high", "low", "close", "volume"]  # no adj_close
    assert bars["close"].iloc[0] == 100.0  # raw close, not the 98.0 adjusted close
    analysis = CandlestickEngine().analyze(bars, instrument_id=instrument_id)
    assert len(analysis.geometry) == 5


def test_loader_unknown_symbol(session: Session) -> None:
    with pytest.raises(LookupError):
        load_closed_bars(session, "NOPE")
