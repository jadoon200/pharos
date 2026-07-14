"""Track building — turn raw AIS position reports into per-vessel voyages.

A vessel's positions arrive as an unordered stream; the detectors and the anomaly model need
*tracks*: contiguous voyages, split at AIS gaps, with clean kinematics and a fixed-length shape
descriptor. This module:

1. **segments** each vessel's positions into voyages, splitting when the silence between two
   reports exceeds `track_gap_split_minutes` (the gap itself is what the dark-ship detector later
   inspects — see `detect/gaps.py`);
2. computes **kinematics** (voyage distance, the largest implied speed between consecutive
   reports — the spoofing signal);
3. builds a **heading-invariant, region-agnostic feature vector** per voyage (resample to a fixed
   length, translate to the origin, rotate so the net displacement points along +x, scale to km)
   so the anomaly model learns *route shape* — a straight transit looks the same in the Singapore
   Strait and off California, which is exactly what makes cross-region generalization possible.

A rebuild is idempotent: it clears the region's incidents (they reference tracks) and tracks,
then recreates the tracks.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import pairwise

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from pharos.config import get_settings
from pharos.db.base import session_scope
from pharos.db.models import Incident, Position, Track
from pharos.geo import haversine_series_km, implied_speed_kn
from pharos.logging import configure_logging, get_logger

log = get_logger(__name__)


def segment(positions: list[Position], gap_minutes: float) -> list[list[Position]]:
    """Split one vessel's positions (any order) into voyages at silences > gap_minutes."""
    pts = sorted(positions, key=lambda p: p.ts)
    if not pts:
        return []
    voyages: list[list[Position]] = [[pts[0]]]
    for prev, cur in pairwise(pts):
        gap = (cur.ts - prev.ts).total_seconds() / 60.0
        if gap > gap_minutes:
            voyages.append([cur])
        else:
            voyages[-1].append(cur)
    return voyages


def _resample_xy(
    lats: NDArray[np.float64], lons: NDArray[np.float64], times: NDArray[np.float64], n: int
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Resample (lat, lon) to `n` time-equidistant points by linear interpolation."""
    if times[-1] <= times[0]:
        # Degenerate (all same time): just repeat the first point.
        return np.full(n, lats[0]), np.full(n, lons[0])
    grid = np.linspace(times[0], times[-1], n)
    return np.interp(grid, times, lats), np.interp(grid, times, lons)


def track_features(points: list[Position], seq_len: int) -> list[float]:
    """A heading-invariant, region-agnostic route-shape descriptor.

    Resample to `seq_len` points, translate to the start, rotate so the net displacement lies
    along +x, and express in kilometres — plus a mean-speed channel. Absolute position is
    deliberately excluded so the model learns *shape* (straight vs wandering), not *place*.
    Length is always `2 * seq_len + 1`.
    """
    lats = np.array([p.lat for p in points], dtype=np.float64)
    lons = np.array([p.lon for p in points], dtype=np.float64)
    times = np.array([p.ts.timestamp() for p in points], dtype=np.float64)
    rlats, rlons = _resample_xy(lats, lons, times, seq_len)

    lat0 = float(rlats[0])
    # Local equirectangular projection to km, relative to the start point.
    x = (rlons - rlons[0]) * 111.19 * np.cos(np.radians(lat0))
    y = (rlats - rlats[0]) * 111.19
    # Rotate so the net displacement (last point) points along +x → heading-invariant.
    theta = np.arctan2(y[-1], x[-1])
    c, s = np.cos(-theta), np.sin(-theta)
    xr = c * x - s * y
    yr = s * x + c * y

    speeds = [p.sog for p in points if p.sog is not None]
    mean_speed = float(np.mean(speeds)) if speeds else 0.0

    feats: list[float] = []
    for i in range(seq_len):
        feats.extend([round(float(xr[i]), 4), round(float(yr[i]), 4)])
    feats.append(round(mean_speed, 3))
    return feats


def track_sequence(points: list[Position], seq_len: int) -> NDArray[np.float64]:
    """A per-step *sequence* descriptor for the GRU autoencoder, shape (seq_len-1, 4).

    Resample to `seq_len` points, rotate the whole path to a canonical heading (as in
    `track_features`), then describe each step by `[dx_km, dy_km, step_len_km, turn_rad]` — the
    local motion. Heading-invariant and region-agnostic, but it preserves the *ordered* geometry a
    recurrent model can exploit (a straight run vs a bend vs a detour), which the flattened
    descriptor blurs.
    """
    lats = np.array([p.lat for p in points], dtype=np.float64)
    lons = np.array([p.lon for p in points], dtype=np.float64)
    times = np.array([p.ts.timestamp() for p in points], dtype=np.float64)
    rlats, rlons = _resample_xy(lats, lons, times, seq_len)

    lat0 = float(rlats[0])
    x = (rlons - rlons[0]) * 111.19 * np.cos(np.radians(lat0))
    y = (rlats - rlats[0]) * 111.19
    theta = np.arctan2(y[-1], x[-1]) if (x[-1] or y[-1]) else 0.0
    c, s = np.cos(-theta), np.sin(-theta)
    xr = c * x - s * y
    yr = s * x + c * y

    dx = np.diff(xr)
    dy = np.diff(yr)
    step_len = np.hypot(dx, dy)
    ang = np.arctan2(dy, dx)
    turn = np.diff(ang, prepend=ang[0])  # heading change per step (0 at the first step)
    return np.stack([dx, dy, step_len, turn], axis=1).astype(np.float64)


def kinematics(points: list[Position]) -> dict[str, float]:
    """Voyage distance (km) and the largest implied speed (knots) between consecutive reports."""
    if len(points) < 2:
        return {"distance_km": 0.0, "max_implied_speed_kn": 0.0}
    lats = np.array([p.lat for p in points], dtype=np.float64)
    lons = np.array([p.lon for p in points], dtype=np.float64)
    steps = haversine_series_km(lats, lons)
    max_speed = 0.0
    for prev, cur in pairwise(points):
        secs = (cur.ts - prev.ts).total_seconds()
        max_speed = max(max_speed, implied_speed_kn(prev.lat, prev.lon, cur.lat, cur.lon, secs))
    return {"distance_km": float(steps.sum()), "max_implied_speed_kn": max_speed}


def build_track(points: list[Position], seq_len: int) -> Track | None:
    """Assemble a Track row from an ordered voyage (or None if too short to analyse)."""
    if len(points) < 2:
        return None
    pts = sorted(points, key=lambda p: p.ts)
    kin = kinematics(pts)
    start, end = pts[0], pts[-1]
    return Track(
        track_id=f"{start.mmsi}:{start.ts.isoformat()}",
        mmsi=start.mmsi,
        region=start.region,
        start_ts=start.ts,
        end_ts=end.ts,
        point_count=len(pts),
        distance_km=round(kin["distance_km"], 3),
        start_lat=start.lat,
        start_lon=start.lon,
        end_lat=end.lat,
        end_lon=end.lon,
        features=track_features(pts, seq_len),
        sequence=track_sequence(pts, seq_len).tolist(),
    )


def build_tracks(session: Session, region: str | None = None) -> dict[str, int]:
    """Rebuild tracks (optionally one region). Idempotent: clears incidents + tracks first."""
    settings = get_settings()
    pos_q = select(Position).order_by(Position.mmsi, Position.ts)
    if region is not None:
        pos_q = pos_q.where(Position.region == region)
    positions = list(session.scalars(pos_q))

    by_vessel: dict[str, list[Position]] = defaultdict(list)
    for p in positions:
        by_vessel[p.mmsi].append(p)

    # Clear the region's incidents (they FK tracks) then its tracks, then rebuild.
    inc_del = delete(Incident)
    trk_del = delete(Track)
    if region is not None:
        inc_del = inc_del.where(Incident.region == region)
        trk_del = trk_del.where(Track.region == region)
    session.execute(inc_del)
    session.execute(trk_del)

    made = 0
    voyages = 0
    for mmsi_points in by_vessel.values():
        for voyage in segment(mmsi_points, settings.track_gap_split_minutes):
            voyages += 1
            track = build_track(voyage, settings.anomaly_seq_len)
            if track is not None and track.point_count >= settings.track_min_points:
                session.add(track)
                made += 1
    log.info("tracks_built", region=region, vessels=len(by_vessel), voyages=voyages, tracks=made)
    return {"vessels": len(by_vessel), "voyages": voyages, "tracks": made}


def main() -> None:
    configure_logging()
    with session_scope() as session:
        build_tracks(session)


if __name__ == "__main__":
    main()
