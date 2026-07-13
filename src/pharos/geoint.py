"""GEOINT evidence bridge — expose PHAROS incidents as citable, source-rated evidence.

The composability payoff: PHAROS is the geospatial lane an all-source analyst (e.g. the sibling
ARGUS) can fuse alongside cyber and open-source reporting - "one analyst across cyber + cognitive
+ geospatial." This module shapes a maritime `Incident` into an evidence item whose fields match
ARGUS's `EvidenceItem` (doc_id / title / source / reliability A-F / credibility 1-6 / summary /
published / url), so ARGUS's read-only bridge pattern (the way it already pulls SENTINEL campaigns)
can cite PHAROS incidents with no schema translation - plus geospatial extras (lat/lon/zone/mmsi/
techniques) an all-source tool can use or ignore.

PHAROS only *serves* this; it is read-only, and every item stays human-review decision support.
"""

from __future__ import annotations

from typing import Any

from pharos.db.models import Incident
from pharos.zones import zone_by_id

# Detector → a readable, title-cased threat phrase for the evidence title/summary.
_DETECTOR_PHRASE = {
    "gap": "Dark ship (AIS gap)",
    "rendezvous": "Ship-to-ship transfer",
    "loiter": "Loitering / zone incursion",
    "spoof": "AIS spoofing",
    "anomaly": "Trajectory anomaly",
}


def credibility_from_score(score: float) -> int:
    """Map a detector score [0,1] to a NATO-Admiralty information-credibility 2-6.

    `1` (confirmed) is reserved - a single AIS-derived signal is never "confirmed by other
    sources". Higher score → more credible (lower number).
    """
    if score >= 0.85:
        return 2
    if score >= 0.65:
        return 3
    if score >= 0.45:
        return 4
    if score >= 0.25:
        return 5
    return 6


def incident_summary(inc: Incident) -> str:
    """A one-sentence, human-review evidence summary for an incident."""
    phrase = _DETECTOR_PHRASE.get(inc.detector, inc.incident_type)
    where = ""
    if inc.zone_id and (zone := zone_by_id(inc.zone_id)):
        where = f" in the {zone.name}"
    ev = inc.evidence or {}
    detail = ""
    if inc.detector == "gap":
        detail = (
            f" — silent {ev.get('gap_minutes', '?')} min, reappearing "
            f"{ev.get('displacement_km', '?')} km displaced"
        )
    elif inc.detector == "rendezvous" and inc.counterpart_mmsi:
        detail = f" with MMSI {inc.counterpart_mmsi} for {ev.get('duration_minutes', '?')} min"
    elif inc.detector == "loiter":
        detail = f" — dwelling {ev.get('duration_minutes', '?')} min"
    elif inc.detector == "spoof":
        detail = f" — implied speed {ev.get('implied_speed_kn', '?')} kn (physically impossible)"
    caveat = " A gap may be benign coverage loss." if inc.detector == "gap" else ""
    return (
        f"{phrase}: MMSI {inc.mmsi}{where}{detail}. "
        f"Human-review decision support, not a verdict.{caveat}"
    )


def to_evidence(inc: Incident) -> dict[str, Any]:
    """Shape an incident as an ARGUS-compatible evidence item + geospatial extras."""
    phrase = _DETECTOR_PHRASE.get(inc.detector, inc.incident_type)
    zone = zone_by_id(inc.zone_id) if inc.zone_id else None
    return {
        # ARGUS EvidenceItem fields (map 1:1) --------------------------------------------
        "doc_id": inc.incident_id,
        "title": f"{phrase} - MMSI {inc.mmsi}",
        "source": "PHAROS maritime domain awareness",
        "reliability": inc.reliability,  # Admiralty A-F (AIS confidence)
        "credibility": credibility_from_score(inc.score),  # Admiralty 1-6
        "summary": incident_summary(inc),
        "published": inc.ts_start.isoformat(),
        "url": f"/incidents/{inc.incident_id}",  # resolvable on this API
        # Geospatial extras (an all-source tool may use or ignore) ------------------------
        "kind": "geoint",
        "detector": inc.detector,
        "mmsi": inc.mmsi,
        "counterpart_mmsi": inc.counterpart_mmsi,
        "lat": inc.lat,
        "lon": inc.lon,
        "zone": zone.name if zone else None,
        "techniques": inc.techniques or [],
        "region": inc.region,
    }
