from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from app.data.calendar import TradingCalendar
from app.data.ingestion import IngestionResult, ingest_daily_bars
from app.data.instruments import SPY
from app.database.models import (
    DataQualityEvent,
    IngestionRun,
    Instrument,
    MarketSession,
    PriceBar,
    ProviderSymbol,
)
from tests.factories import AFTER_JULY_2024, StubProvider, regular_rows, row

pytestmark = pytest.mark.integration

START, END = date(2024, 7, 1), date(2024, 7, 9)


@pytest.fixture(scope="module")
def xnys() -> TradingCalendar:
    return TradingCalendar("XNYS")


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


def ingest(
    session: Session,
    provider: StubProvider,
    xnys: TradingCalendar,
    as_of: datetime = AFTER_JULY_2024,
) -> IngestionResult:
    return ingest_daily_bars(session, provider, SPY, START, END, calendar=xnys, as_of=as_of)


def count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_first_run_persists_bars_sessions_and_instrument(
    session: Session, xnys: TradingCalendar
) -> None:
    result = ingest(session, StubProvider(regular_rows()), xnys)

    assert result.status == "succeeded"
    assert (result.bars_received, result.bars_inserted, result.bars_excluded) == (6, 6, 0)
    assert result.sessions_upserted == 6
    assert count(session, PriceBar) == 6
    assert count(session, MarketSession) == 6
    inst = session.scalars(select(Instrument)).one()
    assert (inst.underlying, inst.symbol, inst.exchange) == ("S&P 500", "SPY", "ARCX")
    ps = session.scalars(select(ProviderSymbol)).one()
    assert (ps.provider, ps.provider_symbol) == ("yfinance", "SPY")

    bar = session.scalars(select(PriceBar).order_by(PriceBar.ts)).first()
    assert bar is not None
    assert bar.ts == datetime(2024, 7, 1, 13, 30, tzinfo=UTC)
    assert bar.close == Decimal("100.000000")
    assert bar.adj_close == Decimal("98.000000")
    assert bar.is_closed

    early = session.scalars(
        select(MarketSession).where(MarketSession.session_date == date(2024, 7, 3))
    ).one()
    assert early.is_early_close
    assert early.market_close == datetime(2024, 7, 3, 17, 0, tzinfo=UTC)

    run = session.get(IngestionRun, result.run_id)
    assert run is not None and run.status == "succeeded" and run.finished_at is not None
    assert run.provider_version is not None and run.provider_version.startswith("yfinance")


def test_rerun_is_idempotent(session: Session, xnys: TradingCalendar) -> None:
    provider = StubProvider(regular_rows())
    ingest(session, provider, xnys)
    snapshot = [
        (b.ts, b.close, b.adj_close, b.volume)
        for b in session.scalars(select(PriceBar).order_by(PriceBar.ts))
    ]

    second = ingest(session, provider, xnys)

    assert (second.bars_inserted, second.bars_updated, second.bars_unchanged) == (0, 0, 6)
    assert second.sessions_upserted == 0
    assert second.issues == []
    assert count(session, PriceBar) == 6
    session.expire_all()
    assert snapshot == [
        (b.ts, b.close, b.adj_close, b.volume)
        for b in session.scalars(select(PriceBar).order_by(PriceBar.ts))
    ]
    assert count(session, IngestionRun) == 2


def test_data_quality_issues_are_persisted_with_run(
    session: Session, xnys: TradingCalendar
) -> None:
    rows = regular_rows()
    rows["2024-07-02"] = (101.0, 50.0, 100.0, 101.0, 99.0, 1_000_000)  # high < low -> excluded
    del rows["2024-07-08"]  # missing session
    result = ingest(session, StubProvider(rows), xnys)

    assert result.bars_excluded == 1
    assert count(session, PriceBar) == 4
    events = session.scalars(select(DataQualityEvent).order_by(DataQualityEvent.check_name)).all()
    assert [(e.check_name, e.severity, e.action_taken) for e in events] == [
        ("missing_session", "warning", "recorded_only"),
        ("ohlc_inconsistent", "error", "excluded"),
    ]
    assert all(e.ingestion_run_id == result.run_id for e in events)
    assert all(e.provider == "yfinance" and e.timeframe == "1d" for e in events)
    ohlc = events[1]
    assert ohlc.bar_ts == datetime(2024, 7, 2, 13, 30, tzinfo=UTC)
    assert ohlc.details is not None and ohlc.details["high"] == 50.0
    run = session.get(IngestionRun, result.run_id)
    assert run is not None and run.issues_count == 2


def test_revised_closed_bar_is_updated_and_reported(
    session: Session, xnys: TradingCalendar
) -> None:
    provider = StubProvider(regular_rows())
    ingest(session, provider, xnys)

    provider.rows["2024-07-02"] = row(150.0)  # provider silently changed history
    result = ingest(session, provider, xnys)

    assert (result.bars_updated, result.bars_unchanged) == (1, 5)
    revised = [i for i in result.issues if i.check_name == "bar_revised"]
    assert len(revised) == 1
    assert revised[0].details["close"] == {"old": "101.000000", "new": "150.000000"}
    session.expire_all()
    bar = session.scalars(
        select(PriceBar).where(PriceBar.ts == datetime(2024, 7, 2, 13, 30, tzinfo=UTC))
    ).one()
    assert bar.close == Decimal("150.000000")


def test_adj_close_restatement_is_expected_and_summarised(
    session: Session, xnys: TradingCalendar
) -> None:
    provider = StubProvider(regular_rows())
    ingest(session, provider, xnys)

    provider.rows = {
        d: (*r[:4], r[4] * 0.99, r[5]) for d, r in provider.rows.items()
    }  # new dividend
    result = ingest(session, provider, xnys)

    assert result.bars_updated == 6
    [issue] = result.issues
    assert (issue.check_name, issue.severity) == ("adj_close_restated", "info")
    assert issue.details["bars"] == 6
    assert issue.details["max_rel_change"] == pytest.approx(0.01, abs=1e-6)
    assert issue.details["first_ts"] == "2024-07-01T13:30:00+00:00"


def test_forming_bar_becomes_closed_on_later_run(session: Session, xnys: TradingCalendar) -> None:
    provider = StubProvider(regular_rows())
    during = datetime(2024, 7, 9, 15, 0, tzinfo=UTC)  # 07-09 session in progress
    ingest(session, provider, xnys, as_of=during)
    last = session.scalars(select(PriceBar).order_by(PriceBar.ts.desc())).first()
    assert last is not None and not last.is_closed

    provider.rows["2024-07-09"] = row(110.0)  # final values differ from intraday snapshot
    result = ingest(session, provider, xnys)

    assert result.bars_updated == 1
    assert "bar_revised" not in {i.check_name for i in result.issues}  # forming bar: expected
    session.expire_all()
    last = session.scalars(select(PriceBar).order_by(PriceBar.ts.desc())).first()
    assert last is not None and last.is_closed and last.close == Decimal("110.000000")


def test_provider_failure_is_recorded_and_nothing_is_written(
    session: Session, xnys: TradingCalendar
) -> None:
    result = ingest(
        session, StubProvider(regular_rows(), fail=TimeoutError("read timed out")), xnys
    )

    assert result.status == "failed"
    assert result.error is not None and "TimeoutError" in result.error
    assert count(session, PriceBar) == 0
    run = session.get(IngestionRun, result.run_id)
    assert run is not None and run.status == "failed" and run.error == result.error
    event = session.scalars(select(DataQualityEvent)).one()
    assert (event.check_name, event.severity, event.action_taken) == (
        "provider_failure",
        "critical",
        "ingestion_aborted",
    )


def test_adj_close_float_noise_is_not_a_restatement(
    session: Session, xnys: TradingCalendar
) -> None:
    provider = StubProvider(regular_rows())
    ingest(session, provider, xnys)
    stored = [b.adj_close for b in session.scalars(select(PriceBar).order_by(PriceBar.ts))]

    # Relative change of ~1.5e-6, as observed between consecutive Yahoo requests.
    provider.rows = {d: (*r[:4], r[4] * (1 + 1.5e-6), r[5]) for d, r in provider.rows.items()}
    result = ingest(session, provider, xnys)

    assert (result.bars_updated, result.bars_unchanged) == (0, 6)
    assert result.adj_close_noise_ignored == 6
    assert result.issues == []
    session.expire_all()
    assert stored == [b.adj_close for b in session.scalars(select(PriceBar).order_by(PriceBar.ts))]


def test_adj_close_disappearing_is_a_revision(session: Session, xnys: TradingCalendar) -> None:
    provider = StubProvider(regular_rows())
    ingest(session, provider, xnys)

    r = provider.rows["2024-07-02"]
    provider.rows["2024-07-02"] = (*r[:4], float("nan"), r[5])
    result = ingest(session, provider, xnys)

    revised = [i for i in result.issues if i.check_name == "bar_revised"]
    assert len(revised) == 1
    assert revised[0].details["adj_close"]["new"] == "None"
