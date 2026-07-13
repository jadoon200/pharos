"""Dark-ship / AIS-gap detector.

A vessel that stops transmitting AIS for an anomalous stretch in or near a sensitive area, then
reappears displaced, is the classic smuggling / sanctions-evasion signature. This detector finds
consecutive reports separated by more than `gap_min_minutes` whose endpoints are at least
`gap_min_displacement_km` apart, and scores the gap by its duration, displacement, and zone
sensitivity.

**The honest limitation, handled not hidden:** an AIS gap is frequently a benign
receiver-coverage loss, not evasion. So confidence — and therefore the reliability grade — is
deliberately capped, and Global Fishing Watch's reception-modelled gap events are the eval
cross-check (`docs/EVAL.md`). Every gap incident is a lead for a human, never a verdict.
"""

from __future__ import annotations

from itertools import pairwise

from pharos.config import Settings
from pharos.db.models import Incident, Position
from pharos.detect.base import make_incident, positions_by_vessel
from pharos.geo import haversine_km, implied_speed_kn
from pharos.zones import zone_for


def detect_gaps(positions: list[Position], settings: Settings) -> list[Incident]:
    incidents: list[Incident] = []
    for mmsi, pts in positions_by_vessel(positions).items():
        for prev, cur in pairwise(pts):
            gap_min = (cur.ts - prev.ts).total_seconds() / 60.0
            if gap_min < settings.gap_min_minutes:
                continue
            disp_km = haversine_km(prev.lat, prev.lon, cur.lat, cur.lon)
            if disp_km < settings.gap_min_displacement_km:
                continue
            # Where it went dark; a sensitive zone there raises salience.
            zone = zone_for(prev.lat, prev.lon)
            speed = implied_speed_kn(
                prev.lat, prev.lon, cur.lat, cur.lon, (cur.ts - prev.ts).total_seconds()
            )
            # Score grows with how far past threshold the gap and displacement are.
            dur_factor = min(1.0, gap_min / (settings.gap_min_minutes * 3))
            disp_factor = min(1.0, disp_km / (settings.gap_min_displacement_km * 5))
            score = round(0.4 + 0.3 * dur_factor + 0.3 * disp_factor, 4)
            # Confidence capped — a gap can be a coverage artifact, so never high certainty.
            confidence = 0.35 + (0.15 if zone and zone.sensitive else 0.0)
            incidents.append(
                make_incident(
                    detector="gap",
                    incident_type="dark ship (AIS gap)",
                    mmsi=mmsi,
                    score=score,
                    confidence=confidence,
                    ts_start=prev.ts,
                    ts_end=cur.ts,
                    lat=prev.lat,
                    lon=prev.lon,
                    region=prev.region,
                    techniques=["ais-gap", "going-dark"],
                    evidence={
                        "gap_minutes": round(gap_min, 1),
                        "displacement_km": round(disp_km, 2),
                        "implied_speed_kn": round(speed, 1),
                        "coverage_caveat": "an AIS gap may be a benign reception gap, not evasion",
                    },
                )
            )
    return incidents
