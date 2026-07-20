"""Deterministic external-event to PHAROS track matching (``match-v1``)."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from pharos.config import Settings, get_settings
from pharos.db.base import init_sqlite_schema, session_scope
from pharos.db.models import EventTrackMatch, ExternalEvent, Position, Track
from pharos.geo import haversine_km
from pharos.labels.coverage import intersects_observed, observed_intervals
from pharos.logging import configure_logging
from pharos.timeutil import utc_naive

RULE_VERSION = "match-v1"


@dataclass(frozen=True)
class Candidate:
    track: Track
    identifier_match: bool
    temporal_distance_s: float
    spatial_distance_km: float | None
    rank: float


def _event_end(event: ExternalEvent) -> datetime:
    return event.ts_end or event.ts_start + timedelta(hours=6)


def _temporal_distance(start: datetime, end: datetime, track: Track) -> float:
    if utc_naive(track.end_ts) < utc_naive(start):
        return (utc_naive(start) - utc_naive(track.end_ts)).total_seconds()
    if utc_naive(track.start_ts) > utc_naive(end):
        return (utc_naive(track.start_ts) - utc_naive(end)).total_seconds()
    return 0.0


def _raw_imo(raw: dict[str, Any] | None) -> str | None:
    if not raw:
        return None
    for key in ("imo", "IMO", "imoNumber", "ImoNumber"):
        value = raw.get(key)
        if value:
            return str(value)
    metadata = raw.get("MetaData")
    if isinstance(metadata, dict):
        value = metadata.get("IMO") or metadata.get("imo")
        if value:
            return str(value)
    return None


def _identifier_mmsis(
    session: Session, event: ExternalEvent, scan_start: datetime, scan_end: datetime
) -> set[str]:
    if event.mmsi:
        return {event.mmsi}
    if not event.imo:
        return set()
    positions = session.scalars(
        select(Position).where(Position.ts >= scan_start, Position.ts <= scan_end)
    ).all()
    return {position.mmsi for position in positions if _raw_imo(position.raw) == event.imo}


def _min_spatial_distance(
    session: Session,
    event: ExternalEvent,
    track: Track,
    scan_start: datetime,
    scan_end: datetime,
) -> float | None:
    if event.lat is None or event.lon is None:
        return None
    positions = session.scalars(
        select(Position).where(
            Position.mmsi == track.mmsi,
            Position.ts >= scan_start,
            Position.ts <= scan_end,
        )
    ).all()
    points = [(position.lat, position.lon) for position in positions]
    points.extend(
        (lat, lon)
        for lat, lon in ((track.start_lat, track.start_lon), (track.end_lat, track.end_lon))
        if lat is not None and lon is not None
    )
    if not points:
        return None
    return min(haversine_km(event.lat, event.lon, lat, lon) for lat, lon in points)


def _candidates(session: Session, event: ExternalEvent, settings: Settings) -> list[Candidate]:
    event_end = _event_end(event)
    delta = timedelta(hours=settings.label_match_max_hours)
    scan_start = event.ts_start - delta
    scan_end = event_end + delta
    query = select(Track).where(
        Track.region == settings.collector_region,
        Track.end_ts >= scan_start,
        Track.start_ts <= scan_end,
    )
    tracks = session.scalars(query).all()
    identifiers = _identifier_mmsis(session, event, scan_start, scan_end)
    if identifiers:
        tracks = [track for track in tracks if track.mmsi in identifiers]

    candidates: list[Candidate] = []
    for track in tracks:
        temporal = _temporal_distance(event.ts_start, event_end, track)
        spatial = _min_spatial_distance(session, event, track, scan_start, scan_end)
        identifier_match = track.mmsi in identifiers
        if not identifier_match:
            if event.lat is None or event.lon is None or spatial is None:
                continue
            if spatial > settings.label_match_max_km:
                continue
        rank = temporal / max(settings.label_match_max_hours * 3600.0, 1.0)
        if spatial is not None:
            rank += spatial / max(settings.label_match_max_km, 1e-9)
        candidates.append(Candidate(track, identifier_match, temporal, spatial, rank))
    return sorted(candidates, key=lambda candidate: (candidate.rank, candidate.track.track_id))


def match_event(
    session: Session,
    event: ExternalEvent,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> list[EventTrackMatch]:
    """Replace this event's active-rule decisions and return every written row."""
    intervals = observed_intervals(session, start_at=settings.pilot_start_at, now=now)
    in_coverage = intersects_observed(event.ts_start, _event_end(event), intervals)
    session.execute(delete(EventTrackMatch).where(EventTrackMatch.event_id == event.event_id))

    candidates = _candidates(session, event, settings) if in_coverage else []
    rows: list[EventTrackMatch] = []
    if not candidates:
        rows.append(
            EventTrackMatch(
                match_id=f"{event.event_id}:∅",
                event_id=event.event_id,
                identifier_match=False,
                rule_version=RULE_VERSION,
                status="unmatched",
                in_observed_coverage=in_coverage,
            )
        )
    else:
        ambiguous = False
        if len(candidates) > 1:
            best = max(candidates[0].rank, 1e-9)
            ambiguous = candidates[1].rank <= best * settings.label_match_ambiguity_ratio
        selected = candidates if ambiguous else candidates[:1]
        for candidate in selected:
            rows.append(
                EventTrackMatch(
                    match_id=f"{event.event_id}:{candidate.track.track_id}",
                    event_id=event.event_id,
                    track_id=candidate.track.track_id,
                    identifier_match=candidate.identifier_match,
                    temporal_distance_s=candidate.temporal_distance_s,
                    spatial_distance_km=candidate.spatial_distance_km,
                    rule_version=RULE_VERSION,
                    status="ambiguous" if ambiguous else "matched",
                    in_observed_coverage=True,
                )
            )
    session.add_all(rows)
    session.commit()
    return rows


def match_all_events(session: Session, settings: Settings) -> dict[str, int]:
    counts = {"matched": 0, "ambiguous": 0, "unmatched": 0}
    events = session.scalars(select(ExternalEvent).order_by(ExternalEvent.event_id)).all()
    for event in events:
        for row in match_event(session, event, settings):
            counts[row.status] += 1
    return counts


def assert_every_covered_event_terminates(session: Session) -> None:
    missing = session.scalars(
        select(ExternalEvent)
        .where(
            ~ExternalEvent.event_id.in_(
                select(EventTrackMatch.event_id).where(
                    EventTrackMatch.in_observed_coverage.is_(True)
                )
            )
        )
        .order_by(ExternalEvent.event_id)
    ).all()
    # Events outside coverage legitimately have only an explicit false-coverage row. Narrow the
    # preflight to events whose current match decision says they were in coverage.
    uncovered_ids = {
        row.event_id
        for row in session.scalars(
            select(EventTrackMatch).where(EventTrackMatch.in_observed_coverage.is_(False))
        )
    }
    missing_ids = [event.event_id for event in missing if event.event_id not in uncovered_ids]
    if missing_ids:
        raise RuntimeError(f"covered events missing match decisions: {', '.join(missing_ids)}")


def main() -> None:
    argparse.ArgumentParser(description=__doc__).parse_args()
    configure_logging()
    init_sqlite_schema()
    with session_scope() as session:
        counts = match_all_events(session, get_settings())
    print(" ".join(f"{status}={count}" for status, count in sorted(counts.items())))


if __name__ == "__main__":
    main()
