"""Phase 1 schema: minimum tables for instruments, bars, sessions and data-quality findings.

Design rules (see docs/architecture.md):
- Underlying, instrument, provider ticker and provider are separate concepts.
- All timestamps are TIMESTAMPTZ in UTC. Bar `ts` is the bar OPEN time.
- The database enforces structure only. Price sanity (high >= low, volume >= 0, ...)
  is checked by the data-validation layer, which must LOG problems instead of
  silently fixing or dropping them.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin

ASSET_CLASSES = ("etf", "equity", "index", "future", "fx", "crypto", "commodity")
TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w")
SEVERITIES = ("info", "warning", "error", "critical")
RUN_STATUSES = ("running", "succeeded", "failed")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Instrument(TimestampMixin, Base):
    """A tradable instrument (e.g. SPY) that tracks an underlying (e.g. S&P 500)."""

    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("symbol", "exchange"),
        CheckConstraint(_in("asset_class", ASSET_CLASSES), name="asset_class"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    underlying: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(128))
    asset_class: Mapped[str] = mapped_column(String(16))
    exchange: Mapped[str] = mapped_column(String(16), comment="ISO 10383 MIC, e.g. ARCX")
    currency: Mapped[str] = mapped_column(String(3))
    timezone: Mapped[str] = mapped_column(String(64), comment="IANA tz of the listing venue")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class ProviderSymbol(TimestampMixin, Base):
    """How a given data provider names an instrument (tickers differ across providers)."""

    __tablename__ = "provider_symbols"
    __table_args__ = (
        UniqueConstraint("provider", "provider_symbol"),
        UniqueConstraint("instrument_id", "provider"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))
    provider_symbol: Mapped[str] = mapped_column(String(64))


class PriceBar(Base):
    """OHLCV bar as delivered by a provider. One row per (instrument, provider, timeframe, ts)."""

    __tablename__ = "price_bars"
    __table_args__ = (
        UniqueConstraint("instrument_id", "provider", "timeframe", "ts"),
        CheckConstraint(_in("timeframe", TIMEFRAMES), name="timeframe"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(32))
    timeframe: Mapped[str] = mapped_column(String(8))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), comment="Bar OPEN time, UTC")
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    adj_close: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 6), comment="Split/dividend-adjusted close (total-return studies)"
    )
    volume: Mapped[Decimal] = mapped_column(Numeric(28, 8))
    is_closed: Mapped[bool] = mapped_column(
        Boolean, comment="False while the bar is still forming; never analyse open bars as closed"
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MarketSession(TimestampMixin, Base):
    """Regular trading session for an exchange calendar on a given date."""

    __tablename__ = "market_sessions"
    __table_args__ = (UniqueConstraint("exchange", "session_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange: Mapped[str] = mapped_column(String(16), comment="Calendar MIC, e.g. XNYS")
    session_date: Mapped[date] = mapped_column(Date)
    market_open: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    market_close: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_early_close: Mapped[bool] = mapped_column(Boolean, server_default="false")
    source: Mapped[str] = mapped_column(String(64), comment="Calendar library/provider used")


class IngestionRun(Base):
    """One execution of a provider ingestion: what was requested, from where, and the outcome.

    Gives every stored bar and data-quality event a reproducible provenance.
    """

    __tablename__ = "ingestion_runs"
    __table_args__ = (CheckConstraint(_in("status", RUN_STATUSES), name="status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_version: Mapped[str | None] = mapped_column(String(64))
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id", ondelete="CASCADE"))
    timeframe: Mapped[str] = mapped_column(String(8))
    requested_start: Mapped[date] = mapped_column(Date)
    requested_end: Mapped[date] = mapped_column(Date)
    as_of: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), comment="Decision time: bars closing after this are not closed"
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    bars_received: Mapped[int] = mapped_column(server_default="0")
    bars_inserted: Mapped[int] = mapped_column(server_default="0")
    bars_updated: Mapped[int] = mapped_column(server_default="0")
    bars_unchanged: Mapped[int] = mapped_column(server_default="0")
    bars_excluded: Mapped[int] = mapped_column(server_default="0")
    issues_count: Mapped[int] = mapped_column(server_default="0")
    error: Mapped[str | None] = mapped_column(Text)


class DataQualityEvent(Base):
    """Every data problem is recorded here, together with the action taken (never silent)."""

    __tablename__ = "data_quality_events"
    __table_args__ = (CheckConstraint(_in("severity", SEVERITIES), name="severity"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ingestion_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("ingestion_runs.id", ondelete="SET NULL"), index=True
    )
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    provider: Mapped[str] = mapped_column(String(32))
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL")
    )
    timeframe: Mapped[str | None] = mapped_column(String(8))
    bar_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    check_name: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(Text)
    action_taken: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
