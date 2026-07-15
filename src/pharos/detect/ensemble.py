"""Composite maritime-threat ensemble — the fusion unit.

The five detectors each cover a different threat; on their own they are a flat list of incidents.
This module fuses them the way SENTINEL rolls per-flow alerts up into per-host threats: it groups
every incident by vessel and produces a **maritime-threat rollup** — which detectors agree, the
unioned maritime-threat techniques, the counterpart vessels, the zones touched, and a single
**transparent composite risk score** whose components are all exposed for explainability.

The risk fusion is deliberately not a raw max: it rewards *corroboration* (multiple distinct
detectors on the same vessel), weights by *reliability* (AIS confidence — a lone low-reliability
gap can't manufacture a high score), and bumps for a *sensitive zone*. Every term is returned in
`components`, so the rollup ranks meaningful threats over coincidental single hits — and stays
human-review decision support, never an automated verdict.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pharos.db.base import session_scope
from pharos.db.models import Incident, Vessel
from pharos.detect.anomaly import detect_anomalies
from pharos.detect.run import detect
from pharos.logging import configure_logging, get_logger
from pharos.zones import zone_by_id

log = get_logger(__name__)

# Admiralty grade → numeric weight for the reliability term.
_GRADE_WEIGHT = {"A": 1.0, "B": 0.8, "C": 0.6, "D": 0.4, "E": 0.2, "F": 0.1}
_SEVERITY = ["low", "moderate", "high", "critical"]


@dataclass
class VesselThreat:
    mmsi: str
    name: str | None
    ship_type: str | None
    flag: str | None
    risk: float
    severity: str
    reliability: str
    detectors: list[str]
    techniques: list[str]
    incident_count: int
    incident_ids: list[str]
    counterparts: list[str]
    zones: list[str]
    lat: float | None
    lon: float | None
    first_ts: str | None
    last_ts: str | None
    components: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _severity_from_risk(risk: float) -> str:
    idx = 0 if risk < 0.4 else 1 if risk < 0.6 else 2 if risk < 0.8 else 3
    return _SEVERITY[idx]


def fuse_incidents(mmsi: str, incidents: list[Incident], vessel: Vessel | None) -> VesselThreat:
    """Fuse one vessel's incidents into a composite maritime-threat rollup."""
    detectors = sorted({i.detector for i in incidents})
    techniques = sorted({t for i in incidents for t in (i.techniques or [])})
    counterparts = sorted({i.counterpart_mmsi for i in incidents if i.counterpart_mmsi})
    zone_ids = sorted({i.zone_id for i in incidents if i.zone_id})
    zone_names = [z.name for zid in zone_ids if (z := zone_by_id(zid))]

    max_score = max(i.score for i in incidents)
    best_rel = max(_GRADE_WEIGHT.get(i.reliability, 0.1) for i in incidents)
    diversity = min(1.0, (len(detectors) - 1) / 3.0)  # 1 detector→0, 4+→1
    sensitive = any(z and z.sensitive for zid in zone_ids if (z := zone_by_id(zid)))

    # Transparent composite: severity x corroboration x reliability, +sensitive-zone bump.
    risk = max_score * (0.7 + 0.3 * diversity) * (0.6 + 0.4 * best_rel)
    if sensitive:
        risk = min(1.0, risk + 0.1)
    risk = round(min(1.0, risk), 4)

    # Representative point: the highest-scoring incident with a location.
    located = sorted(
        (i for i in incidents if i.lat is not None), key=lambda i: i.score, reverse=True
    )
    rep = located[0] if located else incidents[0]
    best_grade = min((i.reliability for i in incidents), key=lambda g: -_GRADE_WEIGHT.get(g, 0.1))

    return VesselThreat(
        mmsi=mmsi,
        name=vessel.name if vessel else None,
        ship_type=vessel.ship_type if vessel else None,
        flag=vessel.flag if vessel else None,
        risk=risk,
        severity=_severity_from_risk(risk),
        reliability=best_grade,
        detectors=detectors,
        techniques=techniques,
        incident_count=len(incidents),
        incident_ids=[i.incident_id for i in incidents],
        counterparts=counterparts,
        zones=zone_names,
        lat=rep.lat,
        lon=rep.lon,
        first_ts=min(i.ts_start for i in incidents).isoformat(),
        last_ts=max((i.ts_end or i.ts_start) for i in incidents).isoformat(),
        components={
            "max_score": round(max_score, 4),
            "detector_count": len(detectors),
            "diversity": round(diversity, 3),
            "best_reliability": best_grade,
            "sensitive_zone": sensitive,
        },
    )


def vessel_rollups(session: Session, region: str | None = None) -> list[VesselThreat]:
    """Every vessel with at least one incident, fused into a threat rollup, riskiest first."""
    q = select(Incident)
    if region is not None:
        q = q.where(Incident.region == region)
    incidents = list(session.scalars(q))

    by_vessel: dict[str, list[Incident]] = {}
    for inc in incidents:
        by_vessel.setdefault(inc.mmsi, []).append(inc)

    rollups = [
        fuse_incidents(mmsi, incs, session.get(Vessel, mmsi)) for mmsi, incs in by_vessel.items()
    ]
    rollups.sort(key=lambda t: t.risk, reverse=True)
    return rollups


def run_all(
    session: Session,
    region: str | None = None,
    *,
    model_path: str | Path | None = None,
) -> dict[str, Any]:
    """Run the whole detection stack: deterministic detectors + the anomaly model."""
    det = detect(session, region)
    anom = detect_anomalies(session, region, model_path=model_path)
    return {"deterministic": det, "anomaly": anom}


def main() -> None:
    configure_logging()
    with session_scope() as session:
        stats = run_all(session)
        session.flush()
        rollups = vessel_rollups(session)
    log.info("ensemble_complete", vessels_at_risk=len(rollups), **stats["deterministic"])
    for t in rollups[:10]:
        log.info(
            "threat",
            mmsi=t.mmsi,
            risk=t.risk,
            severity=t.severity,
            detectors=",".join(t.detectors),
            zones=",".join(t.zones),
        )


if __name__ == "__main__":
    main()
