"""ingestion runs

Revision ID: c4cb3e615291
Revises: d77968d9c70f
Create Date: 2026-10-07 03:02:02.248895+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4cb3e615291"
down_revision: str | None = "d77968d9c70f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_version", sa.String(length=64), nullable=True),
        sa.Column("instrument_id", sa.Integer(), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("requested_start", sa.Date(), nullable=False),
        sa.Column("requested_end", sa.Date(), nullable=False),
        sa.Column(
            "as_of",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Decision time: bars closing after this are not closed",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("bars_received", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bars_inserted", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bars_updated", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bars_unchanged", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bars_excluded", sa.Integer(), server_default="0", nullable=False),
        sa.Column("issues_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')", name=op.f("ck_ingestion_runs_status")
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
            name=op.f("fk_ingestion_runs_instrument_id_instruments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_runs")),
    )
    op.add_column(
        "data_quality_events", sa.Column("ingestion_run_id", sa.BigInteger(), nullable=True)
    )
    op.create_index(
        op.f("ix_data_quality_events_ingestion_run_id"),
        "data_quality_events",
        ["ingestion_run_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_data_quality_events_ingestion_run_id_ingestion_runs"),
        "data_quality_events",
        "ingestion_runs",
        ["ingestion_run_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_data_quality_events_ingestion_run_id_ingestion_runs"),
        "data_quality_events",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_data_quality_events_ingestion_run_id"), table_name="data_quality_events")
    op.drop_column("data_quality_events", "ingestion_run_id")
    op.drop_table("ingestion_runs")
