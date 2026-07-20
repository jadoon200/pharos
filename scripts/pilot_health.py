"""Print and append the daily SG-PILOT operational health/drift summary."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import func, select

from pharos.collector.storage import sqlite_database_path, storage_status
from pharos.config import get_settings
from pharos.db.base import init_sqlite_schema, session_scope
from pharos.db.models import (
    CollectorRun,
    CoverageOutage,
    ExternalEvent,
    Incident,
    Position,
    Track,
)
from pharos.labels.coverage import observed_hours, observed_intervals
from pharos.timeutil import utc_aware, utc_naive


def _age_hours(value: datetime | None, now: datetime) -> float | None:
    if value is None:
        return None
    return round(max(0.0, (now - utc_aware(value)).total_seconds() / 3600.0), 3)


def _cycle_duration(log_path: Path) -> float | None:
    if not log_path.exists():
        return None
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-500_000:]
    except OSError:
        return None
    matches = re.findall(r'(?:duration|elapsed|cycle_seconds)["=:\s]+([0-9]+(?:\.[0-9]+)?)', tail)
    return float(matches[-1]) if matches else None


def build_health_summary(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    settings = get_settings()
    with session_scope() as session:
        runs = session.scalars(select(CollectorRun).order_by(CollectorRun.started_at)).all()
        intervals = observed_intervals(session, start_at=settings.pilot_start_at, now=now)
        hours = observed_hours(intervals)
        latest_report = max(
            (run.last_message_at for run in runs if run.last_message_at), default=None
        )
        open_outage = session.scalar(
            select(CoverageOutage)
            .where(CoverageOutage.closed_at.is_(None))
            .order_by(CoverageOutage.opened_at.desc())
        )
        outages_24h = session.scalars(
            select(CoverageOutage).where(CoverageOutage.opened_at >= now - timedelta(hours=24))
        ).all()
        reports = sum(run.report_count for run in runs)
        vessel_count = session.scalar(select(func.count(func.distinct(Position.mmsi)))) or 0
        position_query = (
            select(Position.mmsi, func.max(Position.ts))
            .where(Position.region == settings.collector_region)
            .group_by(Position.mmsi)
        )
        if settings.pilot_start_at is not None:
            position_query = position_query.where(Position.ts >= settings.pilot_start_at)
        latest_position = dict(session.execute(position_query).tuples().all())
        latest_track = dict(
            session.execute(
                select(Track.mmsi, func.max(Track.end_ts))
                .where(Track.region == settings.collector_region)
                .group_by(Track.mmsi)
            )
            .tuples()
            .all()
        )
        dirty = sum(
            mmsi not in latest_track or utc_naive(ts) > utc_naive(latest_track[mmsi])
            for mmsi, ts in latest_position.items()
        )
        incidents_24h = session.scalars(
            select(Incident).where(Incident.ts_start >= now - timedelta(hours=24))
        ).all()
        incident_counts = Counter(incident.detector for incident in incidents_24h)
        pilot_scores = [
            float(score)
            for score in session.scalars(
                select(Incident.score).where(Incident.region == settings.collector_region)
            ).all()
        ]
        recent_scores = [incident.score for incident in incidents_24h]
        label_counts = dict(
            session.execute(
                select(ExternalEvent.source, func.count()).group_by(ExternalEvent.source)
            )
            .tuples()
            .all()
        )

    storage = storage_status(settings)
    db_path = sqlite_database_path(settings.database_url)
    wal_bytes = (
        Path(f"{db_path}-wal").stat().st_size if db_path and Path(f"{db_path}-wal").exists() else 0
    )

    def percentiles(values: list[float]) -> dict[str, float | None]:
        return {
            "p50": round(float(np.percentile(values, 50)), 6) if values else None,
            "p95": round(float(np.percentile(values, 95)), 6) if values else None,
        }

    return {
        "generated_at": now.isoformat(),
        "source_report_age_hours": _age_hours(latest_report, now),
        "open_outage": {
            "id": open_outage.id,
            "reason": open_outage.reason,
            "age_hours": _age_hours(open_outage.opened_at, now),
        }
        if open_outage
        else None,
        "outages_opened_24h": len(outages_24h),
        "observed_hours": round(hours, 3),
        "reports_per_observed_hour": round(reports / hours, 3) if hours else None,
        "vessels_per_observed_hour": round(vessel_count / hours, 3) if hours else None,
        "dirty_backlog_vessels": dirty,
        "storage": {
            "db_wal_shm_gib": round(storage.gib_used, 4),
            "wal_mib": round(wal_bytes / 1024**2, 2),
            "level": storage.level,
            "warning_gib": settings.storage_warn_gb,
            "hard_gib": settings.storage_hard_gb,
        },
        "incremental_cycle_seconds": _cycle_duration(
            Path.home() / "Library" / "Logs" / "pharos-collector.log"
        ),
        "incidents_24h_by_detector": dict(sorted(incident_counts.items())),
        "score_drift": {
            "recent_24h": percentiles(recent_scores),
            "pilot_to_date": percentiles(pilot_scores),
        },
        "external_labels_by_source": dict(sorted(label_counts.items())),
    }


def main() -> None:
    init_sqlite_schema()
    summary = build_health_summary()
    rendered = json.dumps(summary, indent=2, sort_keys=True)
    print(rendered)
    path = Path("data/pilot-health.log")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
