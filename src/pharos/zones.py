"""Curated maritime reference-zone registry.

The geospatial analogue of ARGUS's source registry: a small, transparent set of maritime
areas — chokepoints, ports, EEZs, protected/disputed waters — each a coarse (lat, lon) ring.
Detectors use `zone_for()` to contextualise a position (a gap inside a sensitive chokepoint
matters more than one in open ocean), and the API serves the rings as GeoJSON for the map.

Rings are deliberately coarse rectangles — enough to attribute an incident to a named area,
not survey-grade boundaries. `sensitive=True` marks watch areas that weight the risk score.
Singapore/Malacca zones are the operational focus; two US zones cover the reproducible NOAA
Marine Cadastre data (US waters) so the offline eval path has zones to exercise.
"""

from __future__ import annotations

from dataclasses import dataclass

from pharos.geo import point_in_polygon


@dataclass(frozen=True)
class ZoneInfo:
    zone_id: str
    name: str
    kind: str  # chokepoint | port | eez | protected | lane
    country: str | None
    polygon: list[list[float]]  # [[lat, lon], ...]
    sensitive: bool = False
    notes: str | None = None


# Coarse rings, [lat, lon] corners. Singapore/Malacca first (the operational focus),
# then US zones for the reproducible NOAA path.
REGISTRY: list[ZoneInfo] = [
    ZoneInfo(
        "singapore-strait",
        "Singapore Strait",
        "chokepoint",
        "SG",
        [[1.10, 103.55], [1.10, 104.10], [1.30, 104.10], [1.30, 103.55]],
        sensitive=True,
        notes="One of the world's busiest and most strategically sensitive chokepoints.",
    ),
    ZoneInfo(
        "malacca-strait",
        "Strait of Malacca (approaches)",
        "chokepoint",
        None,
        [[1.20, 102.20], [1.20, 103.60], [3.20, 100.60], [3.20, 101.90]],
        sensitive=True,
        notes="The Malacca approaches — piracy, smuggling and dark-fleet transit corridor.",
    ),
    ZoneInfo(
        "singapore-anchorages",
        "Singapore Port Anchorages",
        "port",
        "SG",
        [[1.18, 103.70], [1.18, 104.00], [1.28, 104.00], [1.28, 103.70]],
        notes="Port limits / designated anchorages off Singapore.",
    ),
    ZoneInfo(
        "phillip-channel",
        "Phillip Channel",
        "chokepoint",
        "SG",
        [[1.13, 103.72], [1.13, 103.92], [1.22, 103.92], [1.22, 103.72]],
        sensitive=True,
        notes="Narrowest stretch of the Singapore Strait — frequent STS and dark-ship activity.",
    ),
    ZoneInfo(
        "south-china-sea",
        "South China Sea (central)",
        "protected",
        None,
        [[4.0, 109.0], [4.0, 118.0], [16.0, 118.0], [16.0, 109.0]],
        sensitive=True,
        notes="Contested waters — gray-zone maritime activity and disputed EEZ claims.",
    ),
    # --- US zones (the free NOAA Marine Cadastre bulk data covers US waters) ---
    ZoneInfo(
        "us-la-longbeach",
        "Los Angeles / Long Beach Approaches",
        "port",
        "US",
        [[33.55, -118.35], [33.55, -118.05], [33.80, -118.05], [33.80, -118.35]],
        notes="Major US west-coast port complex — anchorage congestion and loitering.",
    ),
    ZoneInfo(
        "us-gulf-lease",
        "Gulf of Mexico (offshore lease blocks)",
        "protected",
        "US",
        [[27.0, -94.0], [27.0, -89.0], [29.5, -89.0], [29.5, -94.0]],
        sensitive=True,
        notes="Offshore energy infrastructure — incursion and loitering watch area.",
    ),
]

_BY_ID: dict[str, ZoneInfo] = {z.zone_id: z for z in REGISTRY}


def all_zones() -> list[ZoneInfo]:
    return list(REGISTRY)


def zone_by_id(zone_id: str) -> ZoneInfo | None:
    return _BY_ID.get(zone_id)


def zone_for(lat: float, lon: float, *, sensitive_only: bool = False) -> ZoneInfo | None:
    """The first registry zone containing (lat, lon), or None.

    Sensitive zones are tested first so a point inside an overlapping watch area is
    attributed to it. `sensitive_only` restricts the search to watch areas.
    """
    ordered = sorted(REGISTRY, key=lambda z: not z.sensitive)
    for zone in ordered:
        if sensitive_only and not zone.sensitive:
            continue
        if point_in_polygon(lat, lon, [(p[0], p[1]) for p in zone.polygon]):
            return zone
    return None
