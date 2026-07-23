"""Non-destructive dirty-vessel track updates for the continuous collector."""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import numpy as np
import torch
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from pharos.config import Settings, get_settings
from pharos.db.base import session_scope
from pharos.db.models import CoverageOutage, Incident, Position, Track
from pharos.detect.anomaly import normalized_scores
from pharos.detect.base import make_incident
from pharos.detect.coverage import CoverageModel
from pharos.detect.gaps import detect_gaps
from pharos.detect.loiter import detect_loitering
from pharos.detect.rendezvous import detect_rendezvous
from pharos.detect.run import DETERMINISTIC_DETECTORS
from pharos.detect.seq_anomaly import SequenceAnomalyModel, model_artifact_path
from pharos.detect.spoof import detect_spoofing
from pharos.logging import configure_logging, get_logger
from pharos.tracks.build import build_track, segment

log = get_logger(__name__)


def _latest_track(session: Session, mmsi: str, region: str | None) -> Track | None:
    query = select(Track).where(Track.mmsi == mmsi)
    if region is not None:
        query = query.where(Track.region == region)
    return session.scalar(query.order_by(Track.start_ts.desc()).limit(1))


def _tail_positions(
    session: Session,
    mmsi: str,
    region: str | None,
    boundary: Track | None,
    not_before: datetime | None = None,
) -> list[Position]:
    query = select(Position).where(Position.mmsi == mmsi)
    if region is not None:
        query = query.where(Position.region == region)
    if not_before is not None:
        query = query.where(Position.ts >= not_before)
    if boundary is not None:
        # Rebuild the latest (still-growing) voyage from its stable start. If a new AIS gap has
        # occurred, segmentation updates that voyage and appends the new one without touching
        # any completed predecessor or another vessel's tracks.
        query = query.where(Position.ts >= boundary.start_ts)
    return list(session.scalars(query.order_by(Position.ts)))


def _upsert_track(session: Session, candidate: Track) -> bool:
    """Insert or update one deterministic track id; return True when newly inserted."""
    existing = session.get(Track, candidate.track_id)
    if existing is None:
        session.add(candidate)
        return True
    existing.region = candidate.region
    existing.start_ts = candidate.start_ts
    existing.end_ts = candidate.end_ts
    existing.point_count = candidate.point_count
    existing.distance_km = candidate.distance_km
    existing.start_lat = candidate.start_lat
    existing.start_lon = candidate.start_lon
    existing.end_lat = candidate.end_lat
    existing.end_lon = candidate.end_lon
    existing.features = candidate.features
    existing.sequence = candidate.sequence
    return False


def rebuild_dirty_tracks(
    session: Session,
    dirty_mmsis: set[str],
    *,
    region: str | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Re-segment and upsert only affected track tails, never clearing tracks/incidents."""
    active_settings = settings or get_settings()
    created = updated = tracks = positions_loaded = 0
    for mmsi in sorted(dirty_mmsis):
        boundary = _latest_track(session, mmsi, region)
        positions = _tail_positions(
            session,
            mmsi,
            region,
            boundary,
            active_settings.pilot_start_at,
        )
        positions_loaded += len(positions)
        for voyage in segment(positions, active_settings.track_gap_split_minutes):
            candidate = build_track(voyage, active_settings.anomaly_seq_len)
            if candidate is None or candidate.point_count < active_settings.track_min_points:
                continue
            tracks += 1
            if _upsert_track(session, candidate):
                created += 1
            else:
                updated += 1
    session.flush()
    log.info(
        "incremental_tracks_complete",
        region=region,
        dirty_vessels=len(dirty_mmsis),
        positions_loaded=positions_loaded,
        tracks=tracks,
        created=created,
        updated=updated,
    )
    return {
        "dirty_vessels": len(dirty_mmsis),
        "positions_loaded": positions_loaded,
        "tracks": tracks,
        "created": created,
        "updated": updated,
    }


def _region_positions(
    session: Session,
    region: str | None,
    settings: Settings,
) -> list[Position]:
    """Every pilot-window position in the region — deliberately NOT time-scoped further.

    The incremental cycle deletes every deterministic incident touching a dirty vessel and
    regenerates them, so detectors must see the vessel's full retained history, and rendezvous
    partner-degree suppression is defined over the whole window. Retention (`make prune`)
    is what bounds this load; narrowing it here would silently drop regenerated incidents.
    """
    query = select(Position)
    if region is not None:
        query = query.where(Position.region == region)
    if settings.pilot_start_at is not None:
        query = query.where(Position.ts >= settings.pilot_start_at)
    return list(session.scalars(query.order_by(Position.mmsi, Position.ts)))


def rebuild_dirty_incidents(
    session: Session,
    dirty_mmsis: set[str],
    *,
    region: str | None = None,
    settings: Settings | None = None,
) -> dict[str, int]:
    """Refresh deterministic incidents involving dirty vessels only."""
    if not dirty_mmsis:
        return {"total": 0, **dict.fromkeys(DETERMINISTIC_DETECTORS, 0)}
    active_settings = settings or get_settings()
    all_positions = _region_positions(session, region, active_settings)
    dirty_positions = [position for position in all_positions if position.mmsi in dirty_mmsis]
    outages = [
        (outage.opened_at, outage.closed_at) for outage in session.scalars(select(CoverageOutage))
    ]

    incidents: list[Incident] = []
    # The witness model must see the WHOLE population, not just the dirty subset: coverage is
    # a statement about who else was audible, so scoring a handful of dirty vessels against
    # only their own reports would starve the index and call every gap coverage-explained.
    coverage = CoverageModel.from_positions(all_positions)
    incidents.extend(detect_gaps(dirty_positions, active_settings, outages, coverage=coverage))
    incidents.extend(detect_loitering(dirty_positions, active_settings))
    incidents.extend(detect_spoofing(dirty_positions, active_settings))
    incidents.extend(detect_rendezvous(all_positions, active_settings, focus_mmsis=dirty_mmsis))
    unique = {incident.incident_id: incident for incident in incidents}

    clear = delete(Incident).where(
        Incident.detector.in_(DETERMINISTIC_DETECTORS),
        or_(
            Incident.mmsi.in_(dirty_mmsis),
            Incident.counterpart_mmsi.in_(dirty_mmsis),
        ),
    )
    if region is not None:
        clear = clear.where(Incident.region == region)
    session.execute(clear)
    session.add_all(unique.values())

    counts = {detector: 0 for detector in DETERMINISTIC_DETECTORS}
    for incident in unique.values():
        counts[incident.detector] += 1
    log.info(
        "incremental_detectors_complete",
        region=region,
        dirty_vessels=len(dirty_mmsis),
        total=len(unique),
        **counts,
    )
    return {"total": len(unique), **counts}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def score_dirty_tracks(
    session: Session,
    dirty_mmsis: set[str],
    *,
    region: str | None = None,
    settings: Settings | None = None,
    artifact_path: str | Path | None = None,
) -> dict[str, int | str]:
    """Score affected tracks with the pinned artifact; never train or use a fallback."""
    active_settings = settings or get_settings()
    artifact = (
        Path(artifact_path)
        if artifact_path is not None
        else model_artifact_path(active_settings.anomaly_model_dir)
    )
    actual_sha = _sha256(artifact)
    if active_settings.anomaly_model_sha256 and actual_sha != active_settings.anomaly_model_sha256:
        raise ValueError("frozen anomaly artifact SHA-256 mismatch")

    torch.set_num_threads(2)
    model = SequenceAnomalyModel.load(artifact)
    if model.threshold is None:
        raise ValueError("frozen anomaly artifact has no calibrated threshold")
    query = select(Track).where(
        Track.mmsi.in_(dirty_mmsis),
        Track.sequence.isnot(None),
    )
    if region is not None:
        query = query.where(Track.region == region)
    tracks = [track for track in session.scalars(query) if track.sequence]

    clear = delete(Incident).where(
        Incident.detector == "anomaly",
        Incident.mmsi.in_(dirty_mmsis),
    )
    if region is not None:
        clear = clear.where(Incident.region == region)
    session.execute(clear)

    flagged = 0
    if tracks:
        sequences = np.array([track.sequence for track in tracks], dtype=np.float64)
        raw_scores = model.score(sequences)
        scores = normalized_scores(raw_scores, model.threshold)
        for track, raw_score, score in zip(tracks, raw_scores, scores, strict=True):
            if raw_score <= model.threshold:
                continue
            flagged += 1
            session.add(
                make_incident(
                    detector="anomaly",
                    incident_type="trajectory anomaly",
                    mmsi=track.mmsi,
                    score=float(score),
                    confidence=0.45,
                    ts_start=track.start_ts,
                    ts_end=track.end_ts,
                    lat=track.start_lat,
                    lon=track.start_lon,
                    region=track.region,
                    track_id=track.track_id,
                    techniques=["trajectory-anomaly", "pattern-of-life-deviation"],
                    evidence={
                        "reconstruction_error": round(float(raw_score), 5),
                        "threshold": round(model.threshold, 5),
                        "model": "gru-seq-ae",
                        "model_parameters": model.parameter_count,
                        "artifact_sha256": actual_sha,
                        "model_source": "frozen-artifact",
                    },
                )
            )
    log.info(
        "incremental_anomaly_complete",
        region=region,
        dirty_vessels=len(dirty_mmsis),
        scored=len(tracks),
        flagged=flagged,
        artifact_sha256=actual_sha,
    )
    return {
        "scored": len(tracks),
        "flagged": flagged,
        "artifact_sha256": actual_sha,
    }


def process_dirty_vessels(
    session: Session,
    dirty_mmsis: set[str],
    *,
    region: str | None = None,
    settings: Settings | None = None,
) -> dict[str, object]:
    """Run one non-destructive incremental track/detector/frozen-model cycle.

    Each phase commits its own short transaction. The cycle now runs concurrently with the
    collector's batch flushes, and SQLite holds its single write lock from a transaction's
    first write until commit — one cycle-long transaction would block every flush for the
    whole (minutes-long) detector compute. Phases are idempotent and the dirty set is
    restored on failure, so a partial cycle is safely re-run.
    """
    active_settings = settings or get_settings()
    track_stats = rebuild_dirty_tracks(
        session,
        dirty_mmsis,
        region=region,
        settings=active_settings,
    )
    session.commit()
    detector_stats = rebuild_dirty_incidents(
        session,
        dirty_mmsis,
        region=region,
        settings=active_settings,
    )
    session.commit()
    anomaly_stats = score_dirty_tracks(
        session,
        dirty_mmsis,
        region=region,
        settings=active_settings,
    )
    return {
        "tracks": track_stats,
        "detectors": detector_stats,
        "anomaly": anomaly_stats,
    }


def main() -> None:
    """Manually process every vessel in the configured live pilot window."""
    configure_logging()
    settings = get_settings()
    with session_scope() as session:
        query = select(Position.mmsi).where(Position.region == settings.collector_region).distinct()
        if settings.pilot_start_at is not None:
            query = query.where(Position.ts >= settings.pilot_start_at)
        dirty_mmsis = set(session.scalars(query))
        stats = process_dirty_vessels(
            session,
            dirty_mmsis,
            region=settings.collector_region,
            settings=settings,
        )
    log.info("manual_incremental_complete", dirty_vessels=len(dirty_mmsis), stats=stats)


if __name__ == "__main__":
    main()
