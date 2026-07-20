"""Build the six bounded public JSON snapshots and fail closed on unsafe content."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pharos.config import Settings, get_settings
from pharos.db.base import init_sqlite_schema, session_scope
from pharos.db.models import (
    CollectorRun,
    CoverageOutage,
    EvaluationRun,
    EventTrackMatch,
    ExternalEvent,
    Incident,
    Position,
    Track,
    TrackReview,
    Vessel,
)
from pharos.detect.seq_anomaly import SequenceAnomalyModel, model_artifact_path
from pharos.eval.pilot import FROZEN_ARTIFACT_SHA256
from pharos.geo import simplify_polyline
from pharos.labels.coverage import observed_hours, observed_intervals, observed_vessel_hours
from pharos.labels.external import GFW_ATTRIBUTION
from pharos.timeutil import utc_aware

DISCLAIMER = "Candidates for human review, never automated verdicts."
TRACKS_MAX_BYTES = 1_500_000
OTHER_MAX_BYTES = 512_000

_FORBIDDEN_KEYS = {
    "apikey",
    "api_key",
    "authorization",
    "gfw_token",
    "aisstream_key",
    "messagetype",
    "metadata",
    "traceback",
    "database_url",
    "sqlite_master",
    "raw",
}
_FORBIDDEN_TEXT = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"/(?:Users|home)/[^\s]+"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"sqlite_master|CREATE\s+TABLE|sqlalchemy", re.IGNORECASE),
    re.compile(r"(?:sqlite|postgresql(?:\+psycopg)?):/{1,3}", re.IGNORECASE),
)


def _round_coordinates(value: Any) -> Any:
    if isinstance(value, list):
        return [_round_coordinates(item) for item in value]
    if isinstance(value, float):
        return round(value, 4)
    return value


def sanitize(payload: Any) -> Any:
    """Deep-copy, coordinate-round, and validate a JSON payload; reject rather than redact."""
    settings = get_settings()
    secret_values = [value for value in (settings.aisstream_key, settings.gfw_token) if value]

    def walk(value: Any, key: str | None = None) -> Any:
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return round(value, 4) if key in {"lat", "lon", "latitude", "longitude"} else value
        if isinstance(value, str):
            if any(secret in value for secret in secret_values):
                raise ValueError("snapshot contains a configured secret")
            if any(pattern.search(value) for pattern in _FORBIDDEN_TEXT):
                raise ValueError("snapshot contains forbidden sensitive text")
            return value
        if isinstance(value, list):
            if key == "coordinates":
                return _round_coordinates(value)
            return [walk(item) for item in value]
        if isinstance(value, dict):
            output: dict[str, Any] = {}
            for child_key, child in value.items():
                if not isinstance(child_key, str):
                    raise TypeError("snapshot object keys must be strings")
                if child_key.lower() in _FORBIDDEN_KEYS:
                    raise ValueError(f"snapshot contains forbidden key: {child_key}")
                output[child_key] = walk(child, child_key.lower())
            return output
        raise TypeError(f"snapshot contains non-JSON object: {type(value).__name__}")

    return walk(copy.deepcopy(payload))


def _iso(value: datetime | None) -> str | None:
    return utc_aware(value).isoformat() if value else None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _common(generated_at: datetime, freshness: str) -> dict[str, Any]:
    return {
        "generated_at": generated_at.isoformat(),
        "source": {
            "ais": "AISStream.io beta feed; no service-level agreement",
            "gfw": GFW_ATTRIBUTION,
        },
        "freshness": freshness,
        "human_review_disclaimer": DISCLAIMER,
    }


def _outage_category(reason: str) -> str:
    lowered = reason.lower()
    if "offline" in lowered:
        return "collector-offline"
    if "timeout" in lowered or "connection" in lowered:
        return "source-connectivity"
    if "persistence" in lowered or "database" in lowered:
        return "local-storage"
    return "coverage-unavailable"


def _status(
    session: Session, settings: Settings, now: datetime, intervals: list[Any]
) -> dict[str, Any]:
    runs = session.scalars(select(CollectorRun).order_by(CollectorRun.started_at)).all()
    last_report = max((run.last_message_at for run in runs if run.last_message_at), default=None)
    age_minutes = (
        max(0.0, (now - utc_aware(last_report)).total_seconds() / 60.0)
        if last_report
        else float("inf")
    )
    mode = "live" if age_minutes < 10 else "delayed" if age_minutes <= 30 else "collector offline"
    outage = session.scalar(
        select(CoverageOutage)
        .where(CoverageOutage.closed_at.is_(None))
        .order_by(CoverageOutage.opened_at.desc())
    )
    last_processing = session.scalar(select(func.max(Track.created_at)))
    calendar_start = settings.pilot_start_at or (runs[0].started_at if runs else now)
    return {
        **_common(
            now, "Generated from the local ledger; public track geometry is delayed ≥15 min."
        ),
        "mode": mode,
        "last_source_report_at": _iso(last_report),
        "last_processing_at": _iso(last_processing),
        "observed_hours": round(observed_hours(intervals), 4),
        "calendar_hours": round(
            max(0.0, (now - utc_aware(calendar_start)).total_seconds() / 3600), 4
        ),
        "current_outage": {
            "reason_category": _outage_category(outage.reason),
            "age_minutes": round((now - utc_aware(outage.opened_at)).total_seconds() / 60, 1),
        }
        if outage
        else None,
        "run_count": len(runs),
        "frozen_artifact_sha256": FROZEN_ARTIFACT_SHA256,
    }


def _stats(
    session: Session, settings: Settings, now: datetime, intervals: list[Any]
) -> dict[str, Any]:
    runs = session.scalars(select(CollectorRun)).all()
    by_detector = dict(
        session.execute(select(Incident.detector, func.count()).group_by(Incident.detector))
        .tuples()
        .all()
    )
    incident_count = sum(by_detector.values())
    vessel_hours = observed_vessel_hours(session, intervals, settings.collector_region)
    labels = dict(
        session.execute(select(ExternalEvent.source, func.count()).group_by(ExternalEvent.source))
        .tuples()
        .all()
    )
    reviewed = (
        session.scalar(
            select(func.count())
            .select_from(TrackReview)
            .where(TrackReview.reviewer_id == "primary", TrackReview.reviewed_at.isnot(None))
        )
        or 0
    )
    reviewed_alerts = (
        session.scalar(
            select(func.count(func.distinct(TrackReview.track_id)))
            .join(Incident, Incident.track_id == TrackReview.track_id)
            .where(TrackReview.reviewer_id == "primary", TrackReview.reviewed_at.isnot(None))
        )
        or 0
    )
    outages = session.scalars(select(CoverageOutage)).all()
    return {
        **_common(now, "Counts reflect the snapshot generation time and observed-time ledger."),
        "vessels": session.scalar(select(func.count()).select_from(Vessel)) or 0,
        "accepted_reports": sum(run.report_count for run in runs),
        "tracks": session.scalar(select(func.count()).select_from(Track)) or 0,
        "incidents": incident_count,
        "incidents_by_detector": by_detector,
        "observed_hours": round(observed_hours(intervals), 4),
        "observed_vessel_hours": round(vessel_hours, 4),
        "alert_rate_per_100_observed_vessel_hours": (
            round(incident_count * 100 / vessel_hours, 4) if vessel_hours else None
        ),
        "reconnect_count": max(0, len(runs) - 1),
        "outage_count": len(outages),
        "outage_hours": round(
            sum(
                max(
                    0.0,
                    (
                        utc_aware(outage.closed_at or now) - utc_aware(outage.opened_at)
                    ).total_seconds(),
                )
                for outage in outages
            )
            / 3600,
            4,
        ),
        "labels_by_source": labels,
        "review_progress": {
            "tracks": {"reviewed": reviewed, "target": 200},
            "pharos_alerts": {"reviewed": reviewed_alerts, "target": 50},
        },
    }


def _track_points(session: Session, track: Track) -> tuple[list[float], list[float]]:
    positions = session.scalars(
        select(Position)
        .where(
            Position.mmsi == track.mmsi,
            Position.ts >= track.start_ts,
            Position.ts <= track.end_ts,
        )
        .order_by(Position.ts)
    ).all()
    return [position.lat for position in positions], [position.lon for position in positions]


def _tracks(session: Session, settings: Settings, now: datetime) -> dict[str, Any]:
    cutoff = now - timedelta(minutes=settings.snapshot_delay_minutes)
    tracks = session.scalars(
        select(Track)
        .where(Track.region == settings.collector_region, Track.end_ts <= cutoff)
        .order_by(Track.end_ts.desc())
        .limit(settings.snapshot_track_limit)
    ).all()
    features: list[dict[str, Any]] = []
    for track in tracks:
        if utc_aware(track.end_ts) > cutoff:
            raise RuntimeError("snapshot delay-floor violation")
        lat, lon = _track_points(session, track)
        if len(lat) < 2:
            continue
        simple_lat, simple_lon = simplify_polyline(lat, lon, settings.snapshot_track_tolerance_km)
        if len(simple_lat) > settings.snapshot_points_per_track:
            indices = np.linspace(
                0, len(simple_lat) - 1, settings.snapshot_points_per_track, dtype=int
            )
            simple_lat = [simple_lat[index] for index in indices]
            simple_lon = [simple_lon[index] for index in indices]
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [longitude, latitude]
                        for latitude, longitude in zip(simple_lat, simple_lon, strict=True)
                    ],
                },
                "properties": {
                    "public_track_id": hashlib.sha256(track.track_id.encode()).hexdigest()[:12],
                    "end_ts": _iso(track.end_ts),
                    "duration_minutes": round(
                        (utc_aware(track.end_ts) - utc_aware(track.start_ts)).total_seconds() / 60,
                        1,
                    ),
                    "distance_km": round(track.distance_km or 0.0, 2),
                    "point_count": len(simple_lat),
                },
            }
        )
    return {
        **_common(now, "Completed tracks only; end timestamps are at least 15 minutes old."),
        "type": "FeatureCollection",
        "delay_floor_minutes": settings.snapshot_delay_minutes,
        "features": features,
    }


def _evidence_summary(evidence: dict[str, Any] | None) -> dict[str, float | int]:
    allowed = {
        "duration_minutes",
        "gap_minutes",
        "displacement_km",
        "distance_km",
        "implied_speed_kn",
        "partner_count",
        "point_count",
    }
    return {
        key: value
        for key, value in (evidence or {}).items()
        if key in allowed and isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _incidents(session: Session, settings: Settings, now: datetime) -> dict[str, Any]:
    incidents = session.scalars(
        select(Incident)
        .where(Incident.region == settings.collector_region)
        .order_by(Incident.ts_start.desc(), Incident.score.desc())
        .limit(settings.snapshot_incident_limit)
    ).all()
    matched_tracks = set(
        session.scalars(
            select(EventTrackMatch.track_id).where(
                EventTrackMatch.track_id.isnot(None),
                EventTrackMatch.status.in_(("matched", "ambiguous")),
            )
        ).all()
    )
    reviews = {
        review.track_id: review
        for review in session.scalars(
            select(TrackReview).where(
                TrackReview.reviewer_id == "primary", TrackReview.reviewed_at.isnot(None)
            )
        )
    }
    output: list[dict[str, Any]] = []
    for incident in incidents:
        review = reviews.get(incident.track_id or "")
        if review:
            state = "unresolved" if review.label == "uncertain" else "reviewed"
        elif incident.track_id in matched_tracks:
            state = "corroborated"
        else:
            state = "candidate"
        output.append(
            {
                "public_incident_id": hashlib.sha256(incident.incident_id.encode()).hexdigest()[
                    :12
                ],
                "detector": incident.detector,
                "type": incident.incident_type,
                "score": round(incident.score, 4),
                "reliability_grade": incident.reliability,
                "ts": _iso(incident.ts_start),
                "lat": incident.lat,
                "lon": incident.lon,
                "status": state,
                "evidence_summary": _evidence_summary(incident.evidence),
            }
        )
    return {
        **_common(now, "Recent bounded incident candidates at snapshot generation time."),
        "incidents": output,
    }


def _evaluations(session: Session, now: datetime) -> dict[str, Any]:
    latest = session.scalar(
        select(EvaluationRun).order_by(EvaluationRun.created_at.desc()).limit(1)
    )
    if latest is None:
        evaluation: dict[str, Any] = {
            "pilot_status": "collecting — no pilot evaluation yet",
            "recorded_real_data_validation": {
                "summary": "Gulf/LA real-data validation remains the current non-pilot result.",
                "limitations": "Synthetic metrics are not substituted for Singapore pilot labels.",
            },
        }
    else:
        evaluation = {
            "pilot_status": latest.metrics.get("status", "preliminary"),
            "run_id": latest.run_id,
            "artifact_sha256": latest.artifact_sha256,
            "data_snapshot": latest.data_snapshot,
            "label_versions": latest.label_versions,
            "sampling_design": latest.sampling_design,
            "metrics": latest.metrics,
            "exclusions": latest.exclusions,
            "negatives": latest.negatives,
            "created_at": _iso(latest.created_at),
        }
    return {
        **_common(now, "Latest recorded evaluation run; no live recomputation in publication."),
        "evaluation": evaluation,
    }


def _model(settings: Settings, now: datetime) -> dict[str, Any]:
    artifact = model_artifact_path(settings.anomaly_model_dir)
    sha = _sha256(artifact) if artifact.exists() else FROZEN_ARTIFACT_SHA256
    threshold: float | None = None
    hidden = settings.anomaly_hidden
    parameters = 804
    metadata: dict[str, Any] = {}
    if artifact.exists():
        loaded = SequenceAnomalyModel.load(artifact)
        threshold = loaded.threshold
        hidden = loaded.hidden
        parameters = loaded.parameter_count
        metadata = loaded.metadata
    return {
        **_common(now, "Frozen SG-PILOT-v0 model metadata; not a mutable scoring endpoint."),
        "artifact_source": "frozen-artifact",
        "sha256": sha,
        "parameter_count": parameters,
        "hidden_size": hidden,
        "threshold": threshold,
        "training_window": metadata.get(
            "training_window", "881 real LA and Gulf tracks; frozen before the Singapore window"
        ),
        "normalization_provenance": metadata.get(
            "normalization", "training-partition per-feature mean/std stored in artifact"
        ),
        "freeze_date": "2026-07-16",
        "model_card": "https://github.com/jadoon200/pharos/blob/main/docs/MODEL_CARD.md",
    }


_REQUIRED = {
    "status.json": {"mode", "generated_at", "last_source_report_at", "source", "freshness"},
    "stats.json": {"vessels", "tracks", "incidents", "generated_at", "source", "freshness"},
    "tracks.json": {"type", "features", "generated_at", "source", "freshness"},
    "incidents.json": {"incidents", "generated_at", "source", "freshness"},
    "evaluations.json": {"evaluation", "generated_at", "source", "freshness"},
    "model.json": {"sha256", "threshold", "generated_at", "source", "freshness"},
}


def _validate_file(name: str, payload: dict[str, Any], encoded: bytes) -> None:
    missing = _REQUIRED[name] - payload.keys()
    if missing:
        raise ValueError(f"{name} missing keys: {sorted(missing)}")
    limit = TRACKS_MAX_BYTES if name == "tracks.json" else OTHER_MAX_BYTES
    if len(encoded) > limit:
        raise ValueError(f"{name} exceeds {limit} byte cap")


def build_snapshots(
    session: Session,
    settings: Settings,
    out_dir: Path,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    now = now or datetime.now(UTC)
    intervals = observed_intervals(session, start_at=settings.pilot_start_at, now=now)
    payloads = {
        "status.json": _status(session, settings, now, intervals),
        "stats.json": _stats(session, settings, now, intervals),
        "tracks.json": _tracks(session, settings, now),
        "incidents.json": _incidents(session, settings, now),
        "evaluations.json": _evaluations(session, now),
        "model.json": _model(settings, now),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    sizes: dict[str, int] = {}
    for name, raw in payloads.items():
        payload = sanitize(raw)
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        _validate_file(name, payload, encoded)
        temporary = out_dir / f".{name}.tmp"
        temporary.write_bytes(encoded)
        temporary.replace(out_dir / name)
        sizes[name] = len(encoded)
    return sizes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    init_sqlite_schema()
    settings = get_settings()
    with session_scope() as session:
        sizes = build_snapshots(session, settings, args.out_dir or settings.snapshots_dir)
    print(" ".join(f"{name}={size}" for name, size in sorted(sizes.items())))


if __name__ == "__main__":
    main()
