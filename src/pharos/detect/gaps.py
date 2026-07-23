"""Dark-ship / AIS-gap detector.

A vessel that stops transmitting AIS for an anomalous stretch in or near a sensitive area, then
reappears displaced, is the classic smuggling / sanctions-evasion signature. This detector finds
consecutive reports separated by more than `gap_min_minutes` whose endpoints are at least
`gap_min_displacement_km` apart, and scores the gap by its duration, displacement, and zone
sensitivity.

**The honest limitation, now measured rather than only capped:** an AIS gap is frequently a
benign receiver-coverage loss, not evasion. Confidence stays capped, but when a
`CoverageModel` is supplied the call is additionally graded by what the corpus itself
witnessed — whether *other* vessels were being heard along the corridor while this one was
silent (`pharos.detect.coverage`). That turns the confound from a caveat into a per-incident
discriminator, which matters because the external GFW gap labels never overlapped NOAA's
terrestrial footprint and so could not calibrate it (`docs/EVAL.md`). Every gap incident
remains a lead for a human, never a verdict.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from itertools import pairwise

from pharos.config import Settings
from pharos.db.models import Incident, Position
from pharos.detect.base import make_incident, positions_by_vessel
from pharos.detect.coverage import CONFIDENCE_BY_VERDICT, CoverageModel
from pharos.geo import haversine_km, implied_speed_kn
from pharos.timeutil import utc_naive
from pharos.zones import zone_for

CoverageInterval = tuple[datetime, datetime | None]


def _overlaps_outage(
    start: datetime,
    end: datetime,
    outages: list[CoverageInterval],
) -> bool:
    gap_start = utc_naive(start)
    gap_end = utc_naive(end)
    return any(
        utc_naive(opened_at) < gap_end and (closed_at is None or utc_naive(closed_at) > gap_start)
        for opened_at, closed_at in outages
    )


def detect_gaps(
    positions: list[Position],
    settings: Settings,
    coverage_outages: Iterable[CoverageInterval] = (),
    coverage: CoverageModel | None = None,
) -> list[Incident]:
    """Find dark-ship candidates. With a `coverage` model, each call is additionally graded
    by whether the corpus witnessed other vessels along the corridor while this one was
    silent — a measured coverage discriminator rather than a blanket caveat."""
    outages = list(coverage_outages)
    incidents: list[Incident] = []
    for mmsi, pts in positions_by_vessel(positions).items():
        for prev, cur in pairwise(pts):
            gap_min = (cur.ts - prev.ts).total_seconds() / 60.0
            if gap_min < settings.gap_min_minutes:
                continue
            disp_km = haversine_km(prev.lat, prev.lon, cur.lat, cur.lon)
            if disp_km < settings.gap_min_displacement_km:
                continue
            # Receiver/laptop silence is not vessel behaviour. Suppress the call entirely when
            # any known coverage outage intersects the vessel's silent interval.
            if _overlaps_outage(prev.ts, cur.ts, outages):
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
            evidence: dict[str, object] = {
                "gap_minutes": round(gap_min, 1),
                "displacement_km": round(disp_km, 2),
                "implied_speed_kn": round(speed, 1),
                "coverage_caveat": "an AIS gap may be a benign reception gap, not evasion",
            }
            if coverage is not None:
                # Measured coverage discriminator: did the corpus hear anyone else along
                # the corridor while this vessel was silent?
                assessment = coverage.assess_gap(
                    mmsi, prev.lat, prev.lon, cur.lat, cur.lon, prev.ts, cur.ts
                )
                evidence.update(assessment.as_evidence())
                confidence = min(
                    1.0, confidence * CONFIDENCE_BY_VERDICT.get(assessment.verdict, 1.0)
                )
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
                    evidence=evidence,
                )
            )
    return incidents
