"""PHAROS read-only maritime-domain-awareness API + one stateless inference route.

Everything except POST /score-track is read-only over the maritime knowledge base. /score-track
runs the trajectory-anomaly model on a pasted track, so it carries the rate-limit + concurrency +
size guards from limits.py. GeoJSON endpoints (/zones, /tracks, /vessels/{mmsi}/track) feed the
map dashboard directly. Incidents are decision support for human review, never automated verdicts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pharos import __version__
from pharos.api.limits import ConcurrencyLimiter, RateLimiter, SlotUnavailable
from pharos.api.scoring import score_track_points
from pharos.config import get_settings
from pharos.db.base import get_session_factory, init_sqlite_schema
from pharos.db.models import Incident, Position, Track, Vessel, Zone
from pharos.detect.anomaly import detect_anomalies
from pharos.detect.ensemble import vessel_rollups
from pharos.detect.run import DETERMINISTIC_DETECTORS
from pharos.geoint import to_evidence


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Free single-container deploys run on a SQLite file with no migration step; create the
    # schema on boot (no-op on Postgres, where Alembic owns it).
    init_sqlite_schema()
    yield


settings = get_settings()
app = FastAPI(
    title="PHAROS",
    version=__version__,
    description="Maritime domain awareness & GEOINT",
    lifespan=_lifespan,
)

_allowed = [o.strip() for o in settings.api_allowed_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed or [],
    allow_origin_regex=None if _allowed else r"http://localhost:\d+",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_rate_limiter = RateLimiter(
    settings.api_rate_limit_requests,
    settings.api_rate_limit_window_seconds,
    settings.api_trust_forwarded_header,
)
_concurrency = ConcurrencyLimiter(
    settings.api_inference_concurrency,
    settings.api_inference_acquire_timeout_seconds,
)


def get_db() -> Iterator[Session]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


# --- response models -----------------------------------------------------------------
class Health(BaseModel):
    status: str = "ok"
    version: str


class ScoreTrackRequest(BaseModel):
    points: list[dict[str, Any]]


# --- read-only endpoints -------------------------------------------------------------
@app.get("/health", response_model=Health)
def health() -> Health:
    return Health(version=__version__)


@app.get("/stats")
def stats(db: Session = Depends(get_db)) -> dict[str, Any]:
    def _count(model: Any) -> int:
        return db.scalar(select(func.count()).select_from(model)) or 0

    by_detector = {
        d: db.scalar(select(func.count()).select_from(Incident).where(Incident.detector == d)) or 0
        for d in (*DETERMINISTIC_DETECTORS, "anomaly")
    }
    return {
        "vessels": _count(Vessel),
        "positions": _count(Position),
        "tracks": _count(Track),
        "incidents": _count(Incident),
        "zones": _count(Zone),
        "incidents_by_detector": by_detector,
    }


@app.get("/vessels")
def list_vessels(
    db: Session = Depends(get_db), limit: int = Query(200, le=1000)
) -> list[dict[str, Any]]:
    vessels = db.scalars(select(Vessel).limit(limit)).all()
    return [
        {
            "mmsi": v.mmsi,
            "name": v.name,
            "ship_type": v.ship_type,
            "flag": v.flag,
            "first_seen": v.first_seen.isoformat() if v.first_seen else None,
            "last_seen": v.last_seen.isoformat() if v.last_seen else None,
        }
        for v in vessels
    ]


@app.get("/vessels/{mmsi}/track")
def vessel_track(mmsi: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """A vessel's positions as a GeoJSON LineString (ordered by time)."""
    pts = db.scalars(select(Position).where(Position.mmsi == mmsi).order_by(Position.ts)).all()
    if not pts:
        raise HTTPException(status_code=404, detail="no positions for that MMSI")
    coords = [[p.lon, p.lat] for p in pts]  # GeoJSON is [lon, lat]
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {"mmsi": mmsi, "points": len(coords)},
    }


@app.get("/incidents")
def list_incidents(
    db: Session = Depends(get_db),
    detector: str | None = None,
    region: str | None = None,
    severity: str | None = None,
    limit: int = Query(200, le=1000),
) -> list[dict[str, Any]]:
    q = select(Incident).order_by(Incident.score.desc())
    if detector:
        q = q.where(Incident.detector == detector)
    if region:
        q = q.where(Incident.region == region)
    if severity:
        q = q.where(Incident.severity == severity)
    return [_incident_dict(i) for i in db.scalars(q.limit(limit)).all()]


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    inc = db.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(status_code=404, detail="incident not found")
    return _incident_dict(inc, full=True)


@app.get("/zones")
def zones_geojson(db: Session = Depends(get_db)) -> dict[str, Any]:
    """All reference zones as a GeoJSON FeatureCollection (rings closed for rendering)."""
    features = []
    for z in db.scalars(select(Zone)).all():
        ring = [[p[1], p[0]] for p in z.polygon]  # [lon, lat]
        if ring and ring[0] != ring[-1]:
            ring.append(ring[0])
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring]},
                "properties": {
                    "zone_id": z.zone_id,
                    "name": z.name,
                    "kind": z.kind,
                    "country": z.country,
                    "sensitive": bool(z.sensitive),
                    "notes": z.notes,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


@app.get("/tracks")
def tracks_geojson(
    db: Session = Depends(get_db), region: str | None = None, limit: int = Query(300, le=1000)
) -> dict[str, Any]:
    """Recent voyages as a GeoJSON FeatureCollection of LineStrings for the map."""
    q = select(Track).order_by(Track.start_ts.desc())
    if region:
        q = q.where(Track.region == region)
    features = []
    for t in db.scalars(q.limit(limit)).all():
        pts = db.scalars(
            select(Position)
            .where(Position.mmsi == t.mmsi, Position.ts >= t.start_ts, Position.ts <= t.end_ts)
            .order_by(Position.ts)
        ).all()
        if len(pts) < 2:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[p.lon, p.lat] for p in pts],
                },
                "properties": {"mmsi": t.mmsi, "track_id": t.track_id, "region": t.region},
            }
        )
    return {"type": "FeatureCollection", "features": features}


@app.get("/maritime-picture")
def maritime_picture(
    db: Session = Depends(get_db), region: str | None = None
) -> list[dict[str, Any]]:
    """The composite: every at-risk vessel as a fused threat rollup, riskiest first."""
    return [t.to_dict() for t in vessel_rollups(db, region)]


@app.get("/geoint/evidence")
def geoint_evidence(
    db: Session = Depends(get_db),
    region: str | None = None,
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    limit: int = Query(100, le=500),
) -> list[dict[str, Any]]:
    """Incidents as citable, source-rated GEOINT evidence (ARGUS-compatible `EvidenceItem` shape).

    The composability bridge: an all-source analyst can fuse the maritime/geospatial lane with
    cyber + open-source reporting. Read-only; every item is human-review decision support.
    """
    q = select(Incident).where(Incident.score >= min_score).order_by(Incident.score.desc())
    if region:
        q = q.where(Incident.region == region)
    return [to_evidence(i) for i in db.scalars(q.limit(limit)).all()]


@app.post("/detect")
def run_detection(db: Session = Depends(get_db), region: str | None = None) -> dict[str, Any]:
    """Rebuild the anomaly incidents on demand (the deterministic ones are rebuilt by the CLI).

    Guarded like the inference route — it trains the model. Included so a fresh deploy can
    populate the anomaly lane from the dashboard without the CLI. Not destructive to positions.
    """
    return {"anomaly": detect_anomalies(db, region)}


@app.post("/score-track")
def score_track(
    payload: ScoreTrackRequest, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Stateless inference: score a pasted track's shape through the anomaly model (read-only)."""
    if not _rate_limiter.allow(request):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    if len(str(payload.points)) > settings.api_max_request_chars:
        raise HTTPException(status_code=422, detail="request too large")
    try:
        with _concurrency.slot():
            return score_track_points(db, payload.points)
    except SlotUnavailable as exc:
        raise HTTPException(status_code=503, detail="server busy, try again") from exc


# --- helpers -------------------------------------------------------------------------
def _incident_dict(inc: Incident, full: bool = False) -> dict[str, Any]:
    d = {
        "incident_id": inc.incident_id,
        "mmsi": inc.mmsi,
        "detector": inc.detector,
        "incident_type": inc.incident_type,
        "score": inc.score,
        "severity": inc.severity,
        "reliability": inc.reliability,
        "ts_start": inc.ts_start.isoformat(),
        "ts_end": inc.ts_end.isoformat() if inc.ts_end else None,
        "lat": inc.lat,
        "lon": inc.lon,
        "zone_id": inc.zone_id,
        "counterpart_mmsi": inc.counterpart_mmsi,
        "techniques": inc.techniques,
        "region": inc.region,
    }
    if full:
        d["evidence"] = inc.evidence
    return d


# --- serve the built dashboard (single-container deploy) ------------------------------
# Mounted last so the API routes above take precedence; the SPA is served for everything
# else (html=True → index.html fallback for client-side routing). No-op in dev, where Vite
# serves the frontend on :5173 and this dist/ doesn't exist.
_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(_DIST), html=True), name="spa")
