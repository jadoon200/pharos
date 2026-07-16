"""WAL-safe retention pruning for live AIS positions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, text
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.orm import Session

from pharos.collector.storage import storage_status
from pharos.config import Settings
from pharos.db.models import Position
from pharos.logging import get_logger

log = get_logger(__name__)


def checkpoint(engine: Engine) -> bool:
    """Return True only when SQLite reports an unblocked truncate checkpoint."""
    if engine.dialect.name != "sqlite":
        return True
    with engine.connect() as connection:
        row = connection.execute(text("PRAGMA wal_checkpoint(TRUNCATE)")).one()
    return int(row[0]) == 0


def prune_positions(engine: Engine, settings: Settings) -> dict[str, int | float | str]:
    """Delete only expired live positions, after a successful WAL checkpoint."""
    if not checkpoint(engine):
        raise RuntimeError("WAL checkpoint busy; pruning aborted")
    cutoff = datetime.now(UTC) - timedelta(days=settings.retention_positions_days)
    with Session(engine) as session:
        result = cast(
            CursorResult[Any],
            session.execute(
                delete(Position).where(
                    Position.source == "aisstream",
                    Position.ts < cutoff,
                )
            ),
        )
        deleted = result.rowcount or 0
        session.commit()
    if not checkpoint(engine):
        raise RuntimeError("post-prune WAL checkpoint busy")
    status = storage_status(settings)
    stats: dict[str, int | float | str] = {
        "deleted": deleted,
        "retention_days": settings.retention_positions_days,
        "storage_gib": round(status.gib_used, 3),
        "storage_level": status.level,
    }
    log.info("position_prune_complete", **stats)
    return stats
