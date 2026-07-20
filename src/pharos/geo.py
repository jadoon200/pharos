"""Dependency-light geospatial primitives.

PHAROS deliberately avoids a heavy geo stack (PostGIS / GEOS / shapely) so the whole
system stays testable on in-memory SQLite and installs with no native GIS libraries. The
two operations the detectors and zones need — great-circle distance and point-in-polygon —
are implemented here in pure numpy: haversine for range/speed and ray-casting for geofence
membership. Distances are in kilometres; coordinates are (lat, lon) in decimal degrees.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from numpy.typing import NDArray

EARTH_RADIUS_KM = 6371.0088
KM_PER_NAUTICAL_MILE = 1.852


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two (lat, lon) points, in kilometres."""
    rlat1, rlon1, rlat2, rlon2 = np.radians([lat1, lon1, lat2, lon2])
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = np.sin(dlat / 2.0) ** 2 + np.cos(rlat1) * np.cos(rlat2) * np.sin(dlon / 2.0) ** 2
    return float(2.0 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a)))


def haversine_series_km(
    lats: NDArray[np.float64], lons: NDArray[np.float64]
) -> NDArray[np.float64]:
    """Consecutive great-circle step distances (km) along a track. Length N-1 for N points."""
    rlat = np.radians(lats)
    rlon = np.radians(lons)
    dlat = np.diff(rlat)
    dlon = np.diff(rlon)
    a = np.sin(dlat / 2.0) ** 2 + np.cos(rlat[:-1]) * np.cos(rlat[1:]) * np.sin(dlon / 2.0) ** 2
    steps: NDArray[np.float64] = 2.0 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))
    return steps


def haversine_pairs_km(
    lats1: NDArray[np.float64],
    lons1: NDArray[np.float64],
    lats2: NDArray[np.float64],
    lons2: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Elementwise great-circle distances for aligned coordinate arrays, in kilometres."""
    rlat1 = np.radians(lats1)
    rlon1 = np.radians(lons1)
    rlat2 = np.radians(lats2)
    rlon2 = np.radians(lons2)
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = np.sin(dlat / 2.0) ** 2 + np.cos(rlat1) * np.cos(rlat2) * np.sin(dlon / 2.0) ** 2
    distances: NDArray[np.float64] = 2.0 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))
    return distances


def implied_speed_kn(lat1: float, lon1: float, lat2: float, lon2: float, seconds: float) -> float:
    """Speed (knots) implied by moving between two points in `seconds`.

    The kinematic-plausibility signal behind gap displacement and spoofing: a surface
    vessel cannot exceed a physical ceiling, so an implied speed far above it means the two
    reports cannot both be genuine. Returns 0.0 for a non-positive interval.
    """
    if seconds <= 0:
        return 0.0
    km = haversine_km(lat1, lon1, lat2, lon2)
    return (km / KM_PER_NAUTICAL_MILE) / (seconds / 3600.0)


def bbox_of(polygon: Sequence[tuple[float, float]]) -> tuple[float, float, float, float]:
    """Axis-aligned bounds (min_lat, min_lon, max_lat, max_lon) of a (lat, lon) ring."""
    arr = np.asarray(polygon, dtype=np.float64)
    return (
        float(arr[:, 0].min()),
        float(arr[:, 1].min()),
        float(arr[:, 0].max()),
        float(arr[:, 1].max()),
    )


def point_in_polygon(lat: float, lon: float, polygon: Sequence[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test for a (lat, lon) ring of (lat, lon) vertices.

    A cheap bounding-box reject runs first. The ring may be open or closed (the wrap edge is
    handled). Robust enough for coarse maritime geofences (EEZs, chokepoints, ports); it is
    a planar test in lon/lat, which is fine at the zone sizes PHAROS uses (not near the poles
    or the antimeridian).
    """
    if len(polygon) < 3:
        return False
    min_lat, min_lon, max_lat, max_lon = bbox_of(polygon)
    if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
        return False
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]  # (lat, lon)
        yj, xj = polygon[j]
        # Does a ray going +lon from the point cross this edge?
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def simplify_polyline(
    lat: Sequence[float], lon: Sequence[float], tolerance_km: float
) -> tuple[list[float], list[float]]:
    """Simplify a lat/lon line with deterministic Douglas-Peucker geometry.

    The local equirectangular projection is accurate at the short maritime-track scale and keeps
    this implementation pure numpy. Endpoints are always retained and input order is preserved.
    """
    if len(lat) != len(lon):
        raise ValueError("latitude and longitude lengths differ")
    if tolerance_km < 0:
        raise ValueError("tolerance_km must be non-negative")
    if len(lat) <= 2 or tolerance_km == 0:
        return list(lat), list(lon)

    lats = np.asarray(lat, dtype=np.float64)
    lons = np.asarray(lon, dtype=np.float64)
    mean_lat = float(np.radians(lats.mean()))
    x = np.radians(lons) * EARTH_RADIUS_KM * np.cos(mean_lat)
    y = np.radians(lats) * EARTH_RADIUS_KM
    keep = np.zeros(len(lats), dtype=np.bool_)
    keep[0] = keep[-1] = True
    stack: list[tuple[int, int]] = [(0, len(lats) - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        vx, vy = x[end] - x[start], y[end] - y[start]
        px, py = x[start + 1 : end] - x[start], y[start + 1 : end] - y[start]
        denominator = vx * vx + vy * vy
        if denominator == 0.0:
            distances = np.hypot(px, py)
        else:
            fraction = np.clip((px * vx + py * vy) / denominator, 0.0, 1.0)
            distances = np.hypot(px - fraction * vx, py - fraction * vy)
        relative = int(np.argmax(distances))
        maximum = float(distances[relative])
        if maximum > tolerance_km:
            index = start + 1 + relative
            keep[index] = True
            stack.extend(((start, index), (index, end)))
    return lats[keep].tolist(), lons[keep].tolist()
