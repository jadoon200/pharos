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

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations, pairwise
from math import asin, ceil, cos, degrees, floor, radians, sin

import numpy as np
from numpy.typing import NDArray

from pharos.config import Settings
from pharos.db.models import Incident, Position
from pharos.detect.base import make_incident, positions_by_vessel
from pharos.geo import haversine_pairs_km
from pharos.logging import get_logger
from pharos.zones import in_kind

log = get_logger(__name__)

# Coarse cells only generate candidates; the original exact 5-minute resampling and haversine
# test below still decide whether a rendezvous exists. Thirty-minute time bins match the minimum
# event duration, while quarter-degree cells are large enough that normal vessel motion touches
# only a few cells per bin. Expanded envelopes make the index conservative (false candidates are
# allowed; false exclusions are not).
_INDEX_BIN_SECONDS = 30.0 * 60.0
_INDEX_CELL_DEGREES = 0.25
_EARTH_RADIUS_KM = 6371.0088


@dataclass
class _Candidate:
    a_mmsi: str
    b_mmsi: str
    score: float
    ts_start: datetime
    ts_end: datetime
    lat: float
    lon: float
    region: str | None
    duration_min: float
    min_range_km: float


@dataclass
class _Envelope:
    """A vessel's latitude/longitude bounds while slow within one time bin."""

    min_lat: float
    max_lat: float
    min_lon: float
    max_lon: float

    def include(self, lat: float, lon: float) -> None:
        self.min_lat = min(self.min_lat, lat)
        self.max_lat = max(self.max_lat, lat)
        self.min_lon = min(self.min_lon, lon)
        self.max_lon = max(self.max_lon, lon)


@dataclass(frozen=True)
class _Trajectory:
    """Array form cached once per vessel for repeated exact-pair interpolation."""

    ts: NDArray[np.float64]
    lat: NDArray[np.float64]
    lon: NDArray[np.float64]
    sog: NDArray[np.float64]


def _trajectory(points: list[Position]) -> _Trajectory:
    return _Trajectory(
        ts=np.array([p.ts.timestamp() for p in points], dtype=np.float64),
        lat=np.array([p.lat for p in points], dtype=np.float64),
        lon=np.array([p.lon for p in points], dtype=np.float64),
        sog=np.array([p.sog if p.sog is not None else 0.0 for p in points], dtype=np.float64),
    )


def _interp(
    track: _Trajectory, grid: NDArray[np.float64]
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.float64]]:
    lat = np.interp(grid, track.ts, track.lat)
    lon = np.interp(grid, track.ts, track.lon)
    sog = np.interp(grid, track.ts, track.sog)
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


def _is_transiting(points: list[Position], min_speed_kn: float) -> bool:
    """True if the vessel was under way somewhere on its track (not permanently anchored).

    An STS transfer is between vessels that approach and depart; an anchored vessel that merely
    sits near others is not one. Congested-port anchorages otherwise produce a combinatorial
    explosion of false rendezvous — this is the real-data fix.
    """
    return any((p.sog or 0.0) >= min_speed_kn for p in points)


def _slow_fraction_range(
    start_speed: float, end_speed: float, max_speed_kn: float
) -> tuple[float, float] | None:
    """Fraction of a linearly interpolated segment at or below the rendezvous speed."""
    start_slow = start_speed <= max_speed_kn
    end_slow = end_speed <= max_speed_kn
    if start_slow and end_slow:
        return 0.0, 1.0
    if not start_slow and not end_slow:
        return None
    crossing = (max_speed_kn - start_speed) / (end_speed - start_speed)
    return (0.0, crossing) if start_slow else (crossing, 1.0)


def _include_segment_envelopes(
    bins: dict[int, dict[str, _Envelope]],
    mmsi: str,
    start: Position,
    end: Position,
    max_speed_kn: float,
) -> None:
    """Add the slow portion of one interpolated AIS segment to its time-bin envelopes."""
    start_ts = start.ts.timestamp()
    end_ts = end.ts.timestamp()
    if end_ts <= start_ts:
        return
    slow_range = _slow_fraction_range(
        start.sog if start.sog is not None else 0.0,
        end.sog if end.sog is not None else 0.0,
        max_speed_kn,
    )
    if slow_range is None:
        return

    slow_start = start_ts + slow_range[0] * (end_ts - start_ts)
    slow_end = start_ts + slow_range[1] * (end_ts - start_ts)
    first_bin = floor(slow_start / _INDEX_BIN_SECONDS)
    last_bin = floor(slow_end / _INDEX_BIN_SECONDS)

    def _coordinate(ts: float, start_value: float, end_value: float) -> float:
        fraction = (ts - start_ts) / (end_ts - start_ts)
        return start_value + fraction * (end_value - start_value)

    for bin_id in range(first_bin, last_bin + 1):
        bin_start = bin_id * _INDEX_BIN_SECONDS
        bin_end = bin_start + _INDEX_BIN_SECONDS
        left = max(slow_start, bin_start)
        right = min(slow_end, bin_end)
        if left > right:
            continue
        for ts in (left, right):
            lat = _coordinate(ts, start.lat, end.lat)
            lon = _coordinate(ts, start.lon, end.lon)
            envelope = bins[bin_id].get(mmsi)
            if envelope is None:
                bins[bin_id][mmsi] = _Envelope(lat, lat, lon, lon)
            else:
                envelope.include(lat, lon)


def _wrapped_lon_ranges(low: float, high: float) -> list[tuple[float, float]]:
    """Split an increasing longitude interval into one or two ranges inside [-180, 180]."""
    width = high - low
    if width >= 360.0:
        return [(-180.0, 180.0)]
    start = (low + 180.0) % 360.0 - 180.0
    end = start + width
    if end <= 180.0:
        return [(start, end)]
    return [(start, 180.0), (-180.0, end - 360.0)]


def _expanded_ranges(
    envelope: _Envelope, max_distance_km: float
) -> tuple[float, float, list[tuple[float, float]]]:
    """Conservatively expand a coordinate envelope by a great-circle distance."""
    angular = max_distance_km / _EARTH_RADIUS_KM
    lat_pad = degrees(angular)
    min_lat = max(-90.0, envelope.min_lat - lat_pad)
    max_lat = min(90.0, envelope.max_lat + lat_pad)

    # Maximum same-latitude longitude separation for this central angle. Use the most polar
    # latitude touched by the expanded envelope; at/near a pole every longitude is adjacent.
    polar_lat = min(90.0, max(abs(min_lat), abs(max_lat)))
    denominator = cos(radians(polar_lat))
    ratio = sin(angular / 2.0) / max(denominator, 1e-15)
    lon_pad = 180.0 if ratio >= 1.0 else degrees(2.0 * asin(ratio))
    return (
        min_lat,
        max_lat,
        _wrapped_lon_ranges(envelope.min_lon - lon_pad, envelope.max_lon + lon_pad),
    )


def _spatial_candidate_pairs(
    usable: dict[str, list[Position]], max_distance_km: float, max_speed_kn: float
) -> tuple[set[tuple[str, str]], int]:
    """Conservative slow-motion space/time index for exact rendezvous pair evaluation.

    Each vessel's linearly interpolated slow-motion path is bounded per 30-minute time bin. The
    expanded bounds are inserted into coordinate cells; vessels occupying a common cell and time
    bin become candidates. Any pair that can pass the exact distance/speed test is guaranteed to
    remain, while distant or temporally disjoint pairs never reach expensive pairwise resampling.
    """
    bins: dict[int, dict[str, _Envelope]] = defaultdict(dict)
    for mmsi, points in usable.items():
        for start, end in pairwise(points):
            _include_segment_envelopes(bins, mmsi, start, end, max_speed_kn)

    candidates: set[tuple[str, str]] = set()
    lon_cells = ceil(360.0 / _INDEX_CELL_DEGREES)
    for envelopes in bins.values():
        occupants: dict[tuple[int, int], list[str]] = defaultdict(list)
        for mmsi, envelope in envelopes.items():
            min_lat, max_lat, lon_ranges = _expanded_ranges(envelope, max_distance_km)
            min_lat_cell = floor((min_lat + 90.0) / _INDEX_CELL_DEGREES)
            max_lat_cell = floor((max_lat + 90.0) / _INDEX_CELL_DEGREES)
            occupied: set[tuple[int, int]] = set()
            for low_lon, high_lon in lon_ranges:
                min_lon_cell = floor((low_lon + 180.0) / _INDEX_CELL_DEGREES)
                max_lon_cell = floor((high_lon + 180.0) / _INDEX_CELL_DEGREES)
                for lat_cell in range(min_lat_cell, max_lat_cell + 1):
                    for raw_lon_cell in range(min_lon_cell, max_lon_cell + 1):
                        occupied.add((lat_cell, raw_lon_cell % lon_cells))
            for cell in occupied:
                for other in occupants[cell]:
                    if other != mmsi:
                        pair = (mmsi, other) if mmsi < other else (other, mmsi)
                        candidates.add(pair)
                occupants[cell].append(mmsi)
    return candidates, len(bins)


def detect_rendezvous(
    positions: list[Position], settings: Settings, *, use_candidate_index: bool = True
) -> list[Incident]:
    by_vessel = positions_by_vessel(positions)
    # Only pair vessels with enough points, and only *transiting* vessels (exclude anchored ones).
    usable = {
        m: p
        for m, p in by_vessel.items()
        if len(p) >= 2 and _is_transiting(p, settings.rendezvous_min_transit_speed_kn)
    }
    step_s = settings.track_resample_minutes * 60.0
    trajectories = {mmsi: _trajectory(points) for mmsi, points in usable.items()}
    incidents: list[Incident] = []
    candidates: list[_Candidate] = []

    possible_pairs = len(usable) * (len(usable) - 1) // 2
    if use_candidate_index:
        indexed_pairs, time_bins = _spatial_candidate_pairs(
            usable,
            settings.rendezvous_max_km,
            settings.rendezvous_max_speed_kn,
        )
        pairs = sorted(indexed_pairs)
    else:
        time_bins = 0
        pairs = list(combinations(sorted(usable), 2))
    retained_pct = (100.0 * len(pairs) / possible_pairs) if possible_pairs else 0.0
    log.info(
        "rendezvous_candidate_index",
        enabled=use_candidate_index,
        usable_vessels=len(usable),
        possible_pairs=possible_pairs,
        candidate_pairs=len(pairs),
        retained_pct=round(retained_pct, 3),
        time_bins=time_bins,
    )

    for a_mmsi, b_mmsi in pairs:
        a, b = usable[a_mmsi], usable[b_mmsi]
        start = max(a[0].ts, b[0].ts).timestamp()
        end = min(a[-1].ts, b[-1].ts).timestamp()
        if end - start < settings.rendezvous_min_minutes * 60.0:
            continue

        grid = np.arange(start, end + 1e-6, step_s)
        if grid.size < 2:
            continue
        alat, alon, asog = _interp(trajectories[a_mmsi], grid)
        blat, blon, bsog = _interp(trajectories[b_mmsi], grid)
        dist = haversine_pairs_km(alat, alon, blat, blon)
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
        # Co-location inside a designated port/anchorage is expected congestion, not an STS.
        if in_kind(clat, clon, "port"):
            continue
        dur_factor = min(1.0, duration_min / (settings.rendezvous_min_minutes * 3))
        candidates.append(
            _Candidate(
                a_mmsi=a_mmsi,
                b_mmsi=b_mmsi,
                score=round(0.55 + 0.35 * dur_factor, 4),
                ts_start=datetime.fromtimestamp(float(grid[idx]), tz=UTC),
                ts_end=datetime.fromtimestamp(float(grid[idx + length - 1]), tz=UTC),
                lat=clat,
                lon=clon,
                region=a[0].region,
                duration_min=duration_min,
                min_range_km=round(float(dist[seg].min()), 3),
            )
        )

    # A genuine STS is a discrete pairing; drop pairs touching any vessel that "meets" many
    # others (an anchorage cluster or a GPS-glitchy track). Real-data-driven post-filter.
    degree: Counter[str] = Counter()
    for c in candidates:
        degree[c.a_mmsi] += 1
        degree[c.b_mmsi] += 1
    for c in candidates:
        if degree[c.a_mmsi] > settings.rendezvous_max_partners:
            continue
        if degree[c.b_mmsi] > settings.rendezvous_max_partners:
            continue
        for mmsi, counterpart in ((c.a_mmsi, c.b_mmsi), (c.b_mmsi, c.a_mmsi)):
            incidents.append(
                make_incident(
                    detector="rendezvous",
                    incident_type="ship-to-ship transfer",
                    mmsi=mmsi,
                    score=c.score,
                    confidence=0.6,
                    ts_start=c.ts_start,
                    ts_end=c.ts_end,
                    lat=c.lat,
                    lon=c.lon,
                    region=c.region,
                    counterpart_mmsi=counterpart,
                    techniques=["sts-transfer", "rendezvous"],
                    evidence={
                        "duration_minutes": round(c.duration_min, 1),
                        "min_range_km": c.min_range_km,
                        "counterpart": counterpart,
                    },
                )
            )
    return incidents
