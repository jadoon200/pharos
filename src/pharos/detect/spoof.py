"""AIS spoofing / identity-anomaly detector.

Two signatures, both largely deterministic:

- **Impossible kinematics** — an implied speed between consecutive reports above
  `spoof_max_speed_kn` is physically impossible for a surface vessel, so the two positions
  cannot both be genuine (a location jump / GPS spoof). This is a *certain* positive — the one
  place PHAROS grades reliability near-certain — and gives the eval a deterministic ground truth.
- **Duplicate identity** — the same MMSI reporting from two far-apart places at effectively the
  same time (a cloned/duplicated identity).
"""

from __future__ import annotations

from itertools import pairwise

from pharos.config import Settings
from pharos.db.models import Incident, Position
from pharos.detect.base import make_incident, positions_by_vessel
from pharos.geo import haversine_km, implied_speed_kn


def detect_spoofing(positions: list[Position], settings: Settings) -> list[Incident]:
    incidents: list[Incident] = []
    for mmsi, pts in positions_by_vessel(positions).items():
        for prev, cur in pairwise(pts):
            secs = (cur.ts - prev.ts).total_seconds()
            speed = implied_speed_kn(prev.lat, prev.lon, cur.lat, cur.lon, secs)
            if speed <= settings.spoof_max_speed_kn:
                continue
            disp_km = haversine_km(prev.lat, prev.lon, cur.lat, cur.lon)
            # Score saturates well above the physical ceiling; confidence is high (certain
            # positive) but never A — identity is still self-reported.
            score = round(min(1.0, 0.7 + speed / (settings.spoof_max_speed_kn * 20)), 4)
            same_time = secs < 60
            incidents.append(
                make_incident(
                    detector="spoof",
                    incident_type=("duplicate identity" if same_time else "impossible kinematics"),
                    mmsi=mmsi,
                    score=score,
                    confidence=0.85,
                    ts_start=cur.ts,
                    ts_end=cur.ts,
                    lat=cur.lat,
                    lon=cur.lon,
                    region=cur.region,
                    techniques=["ais-spoof", "impossible-kinematics"],
                    evidence={
                        "implied_speed_kn": round(speed, 1),
                        "jump_km": round(disp_km, 1),
                        "interval_s": round(secs, 1),
                        "physical_ceiling_kn": settings.spoof_max_speed_kn,
                        # An impossible jump says the identity is inconsistent, not that a
                        # vessel is deceiving anyone: duplicated/mistyped MMSIs and garbled
                        # AIS messages produce the same arithmetic. The air sibling states
                        # the equivalent caveat on its ICAO24 spoof calls; a fused view
                        # should not read as more certain on the sea side than the sky.
                        "caveat": "may be two vessels sharing an MMSI or a corrupted AIS message",
                    },
                )
            )
    return incidents
