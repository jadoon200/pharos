"""Attribute incidents to the tracks that contain them.

Deterministic detectors emit position-derived incidents without a ``track_id``; only the anomaly
detector scores stored tracks directly. Review strata, reviewed-alert precision, and external
corroboration all need incident→track attribution, so it is resolved once here: an incident
belongs to the same-MMSI track whose time window overlaps the incident interval (longest overlap
wins; gap incidents that span a voyage split attach to a bounding track deterministically).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from pharos.db.models import Incident, Track
from pharos.timeutil import utc_naive

# External event types with a same-named PHAROS detector; agreement with silver labels is only
# meaningful for these (a GFW port visit has no detector counterpart and must not dilute it).
DETECTABLE_EVENT_TYPES = frozenset({"rendezvous", "loiter", "gap"})


def incident_track_pairs(session: Session, region: str | None = None) -> list[tuple[Incident, str]]:
    """Return each attributable incident paired with its containing track id."""
    incident_query = select(Incident)
    track_query = select(Track)
    if region is not None:
        incident_query = incident_query.where(Incident.region == region)
        track_query = track_query.where(Track.region == region)

    tracks_by_mmsi: dict[str, list[Track]] = {}
    for track in session.scalars(track_query):
        tracks_by_mmsi.setdefault(track.mmsi, []).append(track)

    pairs: list[tuple[Incident, str]] = []
    for incident in session.scalars(incident_query.order_by(Incident.incident_id)):
        if incident.track_id:
            pairs.append((incident, incident.track_id))
            continue
        start = utc_naive(incident.ts_start)
        end = utc_naive(incident.ts_end or incident.ts_start)
        best: tuple[float, str] | None = None
        for track in tracks_by_mmsi.get(incident.mmsi, ()):
            track_start = utc_naive(track.start_ts)
            track_end = utc_naive(track.end_ts)
            if track_start > end or track_end < start:
                continue
            overlap = (min(track_end, end) - max(track_start, start)).total_seconds()
            candidate = (overlap, track.track_id)
            if best is None or candidate > best:
                best = candidate
        if best is not None:
            pairs.append((incident, best[1]))
    return pairs


def detectors_by_track(pairs: list[tuple[Incident, str]]) -> dict[str, set[str]]:
    """Map track id → the set of detectors that fired on it."""
    output: dict[str, set[str]] = {}
    for incident, track_id in pairs:
        output.setdefault(track_id, set()).add(incident.detector)
    return output
