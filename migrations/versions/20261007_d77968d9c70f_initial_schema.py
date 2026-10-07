"""initial schema

Revision ID: d77968d9c70f
Revises:
Create Date: 2026-10-07 02:46:01.855746+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d77968d9c70f"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("underlying", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=True),
        sa.Column("asset_class", sa.String(length=16), nullable=False),
        sa.Column(
            "exchange", sa.String(length=16), nullable=False, comment="ISO 10383 MIC, e.g. ARCX"
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column(
            "timezone", sa.String(length=64), nullable=False, comment="IANA tz of the listing venue"
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "asset_class IN ('etf', 'equity', 'index', 'future', 'fx', 'crypto', 'commodity')",
            name=op.f("ck_instruments_asset_class"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instruments")),
        sa.UniqueConstraint("symbol", "exchange", name=op.f("uq_instruments_symbol_exchange")),
    )
    op.create_table(
        "market_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "exchange", sa.String(length=16), nullable=False, comment="Calendar MIC, e.g. XNYS"
        ),
        sa.Column("session_date", sa.Date(), nullable=False),
        sa.Column("market_open", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_close", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_early_close", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "source", sa.String(length=64), nullable=False, comment="Calendar library/provider used"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_sessions")),
        sa.UniqueConstraint(
            "exchange", "session_date", name=op.f("uq_market_sessions_exchange_session_date")
        ),
    )
    op.create_table(
        "data_quality_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=True),
        sa.Column("timeframe", sa.String(length=8), nullable=True),
        sa.Column("bar_ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("check_name", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("action_taken", sa.String(length=64), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'error', 'critical')",
            name=op.f("ck_data_quality_events_severity"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_data_quality_events_instrument_id_instruments"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_quality_events")),
    )
    op.create_table(
        "price_bars",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, comment="Bar OPEN time, UTC"),
        sa.Column("open", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("high", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("low", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column(
            "adj_close",
            sa.Numeric(precision=18, scale=6),
            nullable=True,
            comment="Split/dividend-adjusted close (total-return studies)",
        ),
        sa.Column("volume", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column(
            "is_closed",
            sa.Boolean(),
            nullable=False,
            comment="False while the bar is still forming; never analyse open bars as closed",
        ),
        sa.Column(
            "ingested_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "timeframe IN ('1m', '5m', '15m', '30m', '1h', '4h', '1d', '1w')",
            name=op.f("ck_price_bars_timeframe"),
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_price_bars_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_price_bars")),
        sa.UniqueConstraint(
            "instrument_id",
            "provider",
            "timeframe",
            "ts",
            name=op.f("uq_price_bars_instrument_id_provider_timeframe_ts"),
        ),
    )
    op.create_table(
        "provider_symbols",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_symbol", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_provider_symbols_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_symbols")),
        sa.UniqueConstraint(
            "instrument_id", "provider", name=op.f("uq_provider_symbols_instrument_id_provider")
        ),
        sa.UniqueConstraint(
            "provider", "provider_symbol", name=op.f("uq_provider_symbols_provider_provider_symbol")
        ),
    )


def downgrade() -> None:
    op.drop_table("provider_symbols")
    op.drop_table("price_bars")
    op.drop_table("data_quality_events")
    op.drop_table("market_sessions")
    op.drop_table("instruments")
