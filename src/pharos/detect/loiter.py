"""Loitering / zone-incursion detector.

A vessel that dwells within a small radius at low speed for a sustained window — especially
inside a sensitive watch zone (a chokepoint, an offshore lease block, a protected area) — is
loitering. This detector finds the maximal span in which a vessel stays within `loiter_radius_km`
of an anchor point at under `loiter_max_speed_kn` for at least `loiter_min_minutes`, and attributes
it to a zone when the anchor falls inside one.
"""

from __future__ import annotations

import numpy as np

from pharos.config import Settings
from pharos.db.models import Incident, Position
from pharos.detect.base import make_incident, positions_by_vessel
from pharos.geo import haversine_km
from pharos.zones import zone_for


def _mean_speed(pts: list[Position]) -> float:
    speeds = [p.sog for p in pts if p.sog is not None]
    return float(np.mean(speeds)) if speeds else 0.0


def detect_loitering(positions: list[Position], settings: Settings) -> list[Incident]:
    incidents: list[Incident] = []
    for mmsi, pts in positions_by_vessel(positions).items():
        i = 0
        n = len(pts)
        while i < n:
            anchor = pts[i]
            j = i
            # Extend while every point stays within the loiter radius of the anchor.
            while (
                j + 1 < n
                and haversine_km(anchor.lat, anchor.lon, pts[j + 1].lat, pts[j + 1].lon)
                <= settings.loiter_radius_km
            ):
                j += 1
            span = pts[i : j + 1]
            duration_min = (span[-1].ts - span[0].ts).total_seconds() / 60.0
            if (
                len(span) >= 2
                and duration_min >= settings.loiter_min_minutes
                and _mean_speed(span) <= settings.loiter_max_speed_kn
            ):
                clat = float(np.mean([p.lat for p in span]))
                clon = float(np.mean([p.lon for p in span]))
                zone = zone_for(clat, clon)
                dur_factor = min(1.0, duration_min / (settings.loiter_min_minutes * 3))
                score = round(0.4 + 0.3 * dur_factor + (0.2 if zone and zone.sensitive else 0.0), 4)
                confidence = 0.5 + (0.2 if zone and zone.sensitive else 0.0)
                incidents.append(
                    make_incident(
                        detector="loiter",
                        incident_type=("zone incursion / loitering" if zone else "loitering"),
                        mmsi=mmsi,
                        score=min(score, 1.0),
                        confidence=confidence,
                        ts_start=span[0].ts,
                        ts_end=span[-1].ts,
                        lat=clat,
                        lon=clon,
                        region=anchor.region,
                        techniques=["loitering"] + (["zone-incursion"] if zone else []),
                        evidence={
                            "duration_minutes": round(duration_min, 1),
                            "radius_km": settings.loiter_radius_km,
                            "mean_speed_kn": round(_mean_speed(span), 2),
                            "points": len(span),
                        },
                    )
                )
                i = j + 1  # don't re-report the same dwell
            else:
                i += 1
    return incidents
