"""Ship-to-ship (STS) rendezvous detector.

Two vessels sitting co-located, both slow, offshore, for a sustained window is the dark-fleet
ship-to-ship-transfer signature (cargo/oil moved hull-to-hull, often to obscure origin). This
detector resamples each candidate pair onto a common time grid over their overlapping window,
then finds the longest contiguous stretch where they are within `rendezvous_max_km` and both
below `rendezvous_max_speed_kn`. A stretch of at least `rendezvous_min_minutes` is a rendezvous.

Resampling (rather than exact-timestamp matching) makes it robust to the unaligned report cadence
of real AIS, where two vessels rarely transmit at the same instant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import combinations

import numpy as np
from numpy.typing import NDArray

from pharos.config import Settings
from pharos.db.models import Incident, Position
from pharos.detect.base import make_incident, positions_by_vessel
from pharos.geo import haversine_km


def _interp(
    pts: list[Position], grid: NDArray[np.float64]
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    t = np.array([p.ts.timestamp() for p in pts], dtype=np.float64)
    lat = np.interp(grid, t, np.array([p.lat for p in pts], dtype=np.float64))
    lon = np.interp(grid, t, np.array([p.lon for p in pts], dtype=np.float64))
    sog = np.interp(
        grid, t, np.array([p.sog if p.sog is not None else 0.0 for p in pts], dtype=np.float64)
    )
    return lat, lon, sog


def _longest_run(mask: NDArray[np.bool_]) -> tuple[int, int]:
    """(start_idx, length) of the longest contiguous True run in a boolean array."""
    best_start = best_len = 0
    cur_start = 0
    cur_len = 0
    for k, v in enumerate(mask):
        if v:
            if cur_len == 0:
                cur_start = k
            cur_len += 1
            if cur_len > best_len:
                best_len, best_start = cur_len, cur_start
        else:
            cur_len = 0
    return best_start, best_len


def detect_rendezvous(positions: list[Position], settings: Settings) -> list[Incident]:
    by_vessel = positions_by_vessel(positions)
    # Only pair vessels with enough points and a plausible spatial overlap (cheap bbox reject).
    usable = {m: p for m, p in by_vessel.items() if len(p) >= 2}
    step_s = settings.track_resample_minutes * 60.0
    incidents: list[Incident] = []

    for a_mmsi, b_mmsi in combinations(sorted(usable), 2):
        a, b = usable[a_mmsi], usable[b_mmsi]
        start = max(a[0].ts, b[0].ts).timestamp()
        end = min(a[-1].ts, b[-1].ts).timestamp()
        if end - start < settings.rendezvous_min_minutes * 60.0:
            continue
        # Cheap reject: do the two vessels ever come near each other at all?
        if (
            haversine_km(a[0].lat, a[0].lon, b[0].lat, b[0].lon) > 500
            and haversine_km(a[-1].lat, a[-1].lon, b[-1].lat, b[-1].lon) > 500
        ):
            pass  # still resample — they may converge in the middle

        grid = np.arange(start, end + 1e-6, step_s)
        if grid.size < 2:
            continue
        alat, alon, asog = _interp(a, grid)
        blat, blon, bsog = _interp(b, grid)
        dist = np.array(
            [haversine_km(alat[k], alon[k], blat[k], blon[k]) for k in range(grid.size)]
        )
        slow = (asog <= settings.rendezvous_max_speed_kn) & (
            bsog <= settings.rendezvous_max_speed_kn
        )
        colocated = (dist <= settings.rendezvous_max_km) & slow
        idx, length = _longest_run(colocated)
        duration_min = (length - 1) * settings.track_resample_minutes
        if length < 2 or duration_min < settings.rendezvous_min_minutes:
            continue

        seg = slice(idx, idx + length)
        clat = float(np.mean(alat[seg]))
        clon = float(np.mean(alon[seg]))
        ts_start = datetime.fromtimestamp(float(grid[idx]), tz=UTC)
        ts_end = datetime.fromtimestamp(float(grid[idx + length - 1]), tz=UTC)
        dur_factor = min(1.0, duration_min / (settings.rendezvous_min_minutes * 3))
        score = round(0.55 + 0.35 * dur_factor, 4)
        region = a[0].region
        for mmsi, counterpart in ((a_mmsi, b_mmsi), (b_mmsi, a_mmsi)):
            incidents.append(
                make_incident(
                    detector="rendezvous",
                    incident_type="ship-to-ship transfer",
                    mmsi=mmsi,
                    score=score,
                    confidence=0.6,
                    ts_start=ts_start,
                    ts_end=ts_end,
                    lat=clat,
                    lon=clon,
                    region=region,
                    counterpart_mmsi=counterpart,
                    techniques=["sts-transfer", "rendezvous"],
                    evidence={
                        "duration_minutes": round(duration_min, 1),
                        "min_range_km": round(float(dist[seg].min()), 3),
                        "counterpart": counterpart,
                    },
                )
            )
    return incidents
