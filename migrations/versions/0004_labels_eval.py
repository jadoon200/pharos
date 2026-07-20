"""add independent labels, blinded reviews, and pilot evaluation records

Revision ID: 0004_labels_eval
Revises: 0003_collector_coverage
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from pharos.db.models import JsonType

revision: str = "0004_labels_eval"
down_revision: str | None = "0003_collector_coverage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "external_events",
        sa.Column("event_id", sa.String(255), primary_key=True),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("source_ref", sa.String(255), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("mmsi", sa.String(16), nullable=True),
        sa.Column("imo", sa.String(16), nullable=True),
        sa.Column("vessel_name", sa.String(255), nullable=True),
        sa.Column("ts_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ts_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("source_confidence", sa.String(32), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attribution", sa.Text(), nullable=False),
        sa.Column("raw", JsonType, nullable=True),
    )
    op.create_index("ix_external_events_source", "external_events", ["source"])
    op.create_index("ix_external_events_mmsi", "external_events", ["mmsi"])

    op.create_table(
        "event_track_matches",
        sa.Column("match_id", sa.String(384), primary_key=True),
        sa.Column(
            "event_id", sa.String(255), sa.ForeignKey("external_events.event_id"), nullable=False
        ),
        sa.Column("track_id", sa.String(96), sa.ForeignKey("tracks.track_id"), nullable=True),
        sa.Column("identifier_match", sa.Boolean(), nullable=False),
        sa.Column("temporal_distance_s", sa.Float(), nullable=True),
        sa.Column("spatial_distance_km", sa.Float(), nullable=True),
        sa.Column("rule_version", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("in_observed_coverage", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_track_matches_event_id", "event_track_matches", ["event_id"])

    op.create_table(
        "track_reviews",
        sa.Column("review_id", sa.String(96), primary_key=True),
        sa.Column("track_id", sa.String(96), sa.ForeignKey("tracks.track_id"), nullable=False),
        sa.Column("queue_order", sa.Integer(), nullable=False),
        sa.Column("stratum", sa.String(64), nullable=False),
        sa.Column("label", sa.String(64), nullable=True),
        sa.Column("subtype", sa.String(128), nullable=True),
        sa.Column("confidence", sa.String(16), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("reviewer_id", sa.String(64), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("adjudication", sa.String(96), nullable=True),
    )
    op.create_index("ix_track_reviews_track_id", "track_reviews", ["track_id"])

    op.create_table(
        "evaluation_runs",
        sa.Column("run_id", sa.String(96), primary_key=True),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("data_snapshot", JsonType, nullable=False),
        sa.Column("label_versions", JsonType, nullable=False),
        sa.Column("sampling_design", JsonType, nullable=False),
        sa.Column("metrics", JsonType, nullable=False),
        sa.Column("exclusions", JsonType, nullable=False),
        sa.Column("negatives", JsonType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("evaluation_runs")
    op.drop_table("track_reviews")
    op.drop_table("event_track_matches")
    op.drop_table("external_events")
