"""Shared detector helpers — grading, severity, and incident construction.

Every detector emits `Incident` rows through `make_incident`, so scoring, the NATO-Admiralty-style
reliability grade (AIS confidence), zone attribution, and id construction stay consistent across
the ensemble. Reliability is deliberately conservative and never `A` — an AIS-derived incident is
always human-review decision support, and a "dark ship" is frequently a benign coverage gap.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from pharos.db.models import Incident, Position
from pharos.zones import ZoneInfo, zone_for


def grade_from_confidence(conf: float) -> str:
    """Map a detector confidence [0,1] to an Admiralty reliability grade B..F.

    `A` (completely reliable) is reserved and never assigned to an AIS-derived signal — the
    coverage confound and self-reported identity mean we cannot certify a maritime incident.
    """
    if conf >= 0.8:
        return "B"
    if conf >= 0.6:
        return "C"
    if conf >= 0.4:
        return "D"
    if conf >= 0.2:
        return "E"
    return "F"


def severity_from_score(score: float, *, sensitive_zone: bool = False) -> str:
    """Bucket a score [0,1] into a severity, bumped one level inside a sensitive watch zone."""
    levels = ["low", "moderate", "high", "critical"]
    idx = 0 if score < 0.4 else 1 if score < 0.6 else 2 if score < 0.8 else 3
    if sensitive_zone:
        idx = min(idx + 1, 3)
    return levels[idx]


def positions_by_vessel(positions: list[Position]) -> dict[str, list[Position]]:
    """Group positions by MMSI, each list sorted by timestamp."""
    grouped: dict[str, list[Position]] = defaultdict(list)
    for p in positions:
        grouped[p.mmsi].append(p)
    for pts in grouped.values():
        pts.sort(key=lambda p: p.ts)
    return grouped


def make_incident(
    *,
    detector: str,
    incident_type: str,
    mmsi: str,
    score: float,
    confidence: float,
    ts_start: datetime,
    ts_end: datetime | None,
    lat: float | None,
    lon: float | None,
    region: str | None,
    techniques: list[str],
    evidence: dict[str, Any],
    counterpart_mmsi: str | None = None,
    track_id: str | None = None,
) -> Incident:
    """Build an Incident with a stable id, zone attribution, severity and reliability grade."""
    zone: ZoneInfo | None = zone_for(lat, lon) if lat is not None and lon is not None else None
    sensitive = bool(zone and zone.sensitive)
    # A sensitive-zone hit corroborates the signal → nudge confidence up (capped).
    graded_conf = min(1.0, confidence + (0.1 if sensitive else 0.0))
    suffix = f"-{counterpart_mmsi}" if counterpart_mmsi else ""
    incident_id = f"{detector}-{mmsi}{suffix}-{int(ts_start.timestamp())}"
    return Incident(
        incident_id=incident_id[:96],
        mmsi=mmsi,
        track_id=track_id,
        detector=detector,
        incident_type=incident_type,
        score=round(score, 4),
        severity=severity_from_score(score, sensitive_zone=sensitive),
        reliability=grade_from_confidence(graded_conf),
        ts_start=ts_start,
        ts_end=ts_end,
        lat=lat,
        lon=lon,
        zone_id=zone.zone_id if zone else None,
        counterpart_mmsi=counterpart_mmsi,
        techniques=techniques,
        evidence={**evidence, "zone": zone.name if zone else None},
        region=region,
    )
