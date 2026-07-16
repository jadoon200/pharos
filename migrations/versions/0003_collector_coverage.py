"""add collector coverage ledger and live-tail position index

Revision ID: 0003_collector_coverage
Revises: 0002_track_sequence
Create Date: 2026-07-16

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_collector_coverage"
down_revision: str | None = "0002_track_sequence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collector_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.String(255), nullable=True),
        sa.Column("report_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("vessel_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
    )
    op.create_index("ix_collector_runs_started_at", "collector_runs", ["started_at"])
    op.create_index("ix_collector_runs_status", "collector_runs", ["status"])

    op.create_table(
        "coverage_outages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("collector_runs.id"),
            nullable=False,
        ),
    )
    op.create_index("ix_coverage_outages_opened_at", "coverage_outages", ["opened_at"])
    op.create_index("ix_coverage_outages_run_id", "coverage_outages", ["run_id"])
    op.create_index("ix_positions_mmsi_ts", "positions", ["mmsi", "ts"])


def downgrade() -> None:
    op.drop_index("ix_positions_mmsi_ts", table_name="positions")
    op.drop_table("coverage_outages")
    op.drop_table("collector_runs")
