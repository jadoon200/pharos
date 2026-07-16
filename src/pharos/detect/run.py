"""Run the deterministic detector ensemble over the corpus and persist incidents.

Loads positions (optionally for one region), runs every deterministic detector (gap, rendezvous,
loiter, spoof), and rebuilds their `Incident` rows. The learned trajectory-anomaly detector (M4)
persists its own `detector="anomaly"` rows separately, so this rebuild only clears the
deterministic detectors' incidents — the two coexist and the ensemble (`detect/ensemble.py`)
fuses them per vessel.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from pharos.config import Settings, get_settings
from pharos.db.base import session_scope
from pharos.db.models import CoverageOutage, Incident, Position
from pharos.detect.gaps import CoverageInterval, detect_gaps
from pharos.detect.loiter import detect_loitering
from pharos.detect.rendezvous import detect_rendezvous
from pharos.detect.spoof import detect_spoofing
from pharos.logging import configure_logging, get_logger

log = get_logger(__name__)

DETERMINISTIC_DETECTORS = ("gap", "rendezvous", "loiter", "spoof")


def run_detectors(
    positions: list[Position],
    settings: Settings,
    coverage_outages: Iterable[CoverageInterval] = (),
) -> list[Incident]:
    """Run the four deterministic detectors and return de-duplicated incidents."""
    incidents: list[Incident] = []
    incidents.extend(detect_gaps(positions, settings, coverage_outages))
    incidents.extend(detect_rendezvous(positions, settings))
    incidents.extend(detect_loitering(positions, settings))
    incidents.extend(detect_spoofing(positions, settings))
    # De-dupe on incident_id (stable per detector+mmsi+ts) — keep the first.
    seen: set[str] = set()
    unique: list[Incident] = []
    for inc in incidents:
        if inc.incident_id in seen:
            continue
        seen.add(inc.incident_id)
        unique.append(inc)
    return unique


def detect(session: Session, region: str | None = None) -> dict[str, int]:
    """Rebuild the deterministic detectors' incidents (optionally for one region)."""
    settings = get_settings()
    pos_q = select(Position)
    if region is not None:
        pos_q = pos_q.where(Position.region == region)
    positions = list(session.scalars(pos_q))
    coverage_outages = [
        (outage.opened_at, outage.closed_at) for outage in session.scalars(select(CoverageOutage))
    ]

    clear = delete(Incident).where(Incident.detector.in_(DETERMINISTIC_DETECTORS))
    if region is not None:
        clear = clear.where(Incident.region == region)
    session.execute(clear)

    incidents = run_detectors(positions, settings, coverage_outages)
    session.add_all(incidents)

    by_detector: dict[str, int] = {d: 0 for d in DETERMINISTIC_DETECTORS}
    for inc in incidents:
        by_detector[inc.detector] += 1
    log.info("detect_complete", region=region, total=len(incidents), **by_detector)
    return {"total": len(incidents), **by_detector}


def main() -> None:
    configure_logging()
    with session_scope() as session:
        detect(session)


if __name__ == "__main__":
    main()
