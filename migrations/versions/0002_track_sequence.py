"""add tracks.sequence (per-step descriptor for the GRU anomaly model)

Revision ID: 0002_track_sequence
Revises: 0001_initial
Create Date: 2026-07-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_track_sequence"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("tracks", sa.Column("sequence", _JSON, nullable=True))


def downgrade() -> None:
    op.drop_column("tracks", "sequence")
