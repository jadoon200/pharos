"""Deterministic synthetic AIS generator with injected, labelled events.

Real NOAA daily files are hundreds of MB — they can't live in git, and they carry no
ground-truth labels for the events PHAROS hunts. So the repo ships this generator: it produces
realistic vessel tracks (normal transits that give the anomaly model a pattern-of-life to learn)
plus a handful of *injected events with known labels* — a dark-ship gap, a ship-to-ship
rendezvous, loitering, an impossible-speed spoof, and a route anomaly.

It is the single source of truth for three things that must agree: the unit-test fixtures, the
baked demo seed, and the eval gold set. Every scenario is seeded, so runs are reproducible. Real
NOAA + Global Fishing Watch data validate these detectors when available (`docs/EVAL.md`); the
synthetic labels make the offline path measurable and deterministic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from pharos.db.models import Position, Vessel
from pharos.ingest.noaa import flag_for_mmsi

KM_PER_DEG_LAT = 111.19
KN_TO_KMH = 1.852


@dataclass(frozen=True)
class GroundTruthEvent:
    """A labelled event injected into a scenario — what a detector *should* find."""

    mmsi: str
    event_type: str  # gap | rendezvous | loiter | spoof | anomaly | normal
    ts_start: datetime
    ts_end: datetime
    lat: float
    lon: float
    counterpart_mmsi: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    region: str
    vessels: list[Vessel]
    positions: list[Position]
    truth: list[GroundTruthEvent]


@dataclass(frozen=True)
class _RegionSpec:
    center: tuple[float, float]
    lane_heading_deg: float  # dominant transit direction
    event_center: tuple[float, float]  # inside a sensitive zone (see pharos.zones)


REGIONS: dict[str, _RegionSpec] = {
    # Singapore Strait / Phillip Channel — the operational focus.
    "singapore": _RegionSpec((1.20, 103.85), 90.0, (1.17, 103.82)),
    # US west coast (LA/Long Beach approaches) — the cross-region generalization partner,
    # and a stand-in for the real NOAA Marine Cadastre coverage (US waters).
    "us-west": _RegionSpec((33.68, -118.20), 200.0, (33.68, -118.20)),
}


def _step(lat: float, lon: float, heading_deg: float, distance_km: float) -> tuple[float, float]:
    """Advance a point `distance_km` along a compass heading (0=N, 90=E)."""
    h = math.radians(heading_deg)
    dlat = (distance_km * math.cos(h)) / KM_PER_DEG_LAT
    dlon = (distance_km * math.sin(h)) / (KM_PER_DEG_LAT * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def _mmsi(region: str, n: int) -> str:
    """A plausible MMSI: SG prefix for the Singapore region, US prefix for us-west."""
    prefix = "563" if region == "singapore" else "366"
    return f"{prefix}{n:06d}"


class _Builder:
    def __init__(self, region: str, seed: int, start: datetime, interval_s: float) -> None:
        self.region = region
        self.spec = REGIONS[region]
        self.rng = np.random.default_rng(seed)
        self.start = start
        self.interval = timedelta(seconds=interval_s)
        self.vessels: list[Vessel] = []
        self.positions: list[Position] = []
        self.truth: list[GroundTruthEvent] = []
        self._n = 0

    def _new_mmsi(self) -> str:
        self._n += 1
        return _mmsi(self.region, self._n)

    def _vessel(self, mmsi: str, ship_type: str, name: str) -> None:
        self.vessels.append(
            Vessel(mmsi=mmsi, name=name, ship_type=ship_type, flag=flag_for_mmsi(mmsi))
        )

    def _emit(self, mmsi: str, ts: datetime, lat: float, lon: float, sog: float) -> None:
        self.positions.append(
            Position(
                mmsi=mmsi,
                ts=ts,
                lat=lat,
                lon=lon,
                sog=round(sog, 1),
                cog=None,
                source="noaa",
                region=self.region,
            )
        )

    def _set_seen(self, mmsi: str) -> None:
        pts = [p for p in self.positions if p.mmsi == mmsi]
        if not pts:
            return
        v = next(v for v in self.vessels if v.mmsi == mmsi)
        v.first_seen = min(p.ts for p in pts)
        v.last_seen = max(p.ts for p in pts)

    # --- track templates -----------------------------------------------------------------
    def normal_transit(self, steps: int = 40, speed: float = 12.0) -> str:
        """A benign straight-line transit along the lane (pattern-of-life for training)."""
        mmsi = self._new_mmsi()
        self._vessel(mmsi, "cargo", f"TRANSIT {self._n}")
        # Start offset around the region center, jitter the exact lane.
        lat, lon = self.spec.center
        lat += float(self.rng.normal(0, 0.02))
        lon += float(self.rng.normal(0, 0.05))
        heading = self.spec.lane_heading_deg + float(self.rng.normal(0, 4))
        ts = self.start + timedelta(minutes=float(self.rng.integers(0, 30)))
        for _ in range(steps):
            self._emit(mmsi, ts, lat, lon, speed + float(self.rng.normal(0, 0.5)))
            dist = speed * KN_TO_KMH * (self.interval.total_seconds() / 3600.0)
            lat, lon = _step(lat, lon, heading, dist)
            ts += self.interval
        self._set_seen(mmsi)
        self.truth.append(GroundTruthEvent(mmsi, "normal", self.positions[-1].ts, ts, lat, lon))
        return mmsi

    def gap_event(self, gap_minutes: float = 180.0, speed: float = 11.0) -> str:
        """Transit, then go dark for `gap_minutes` while still moving → displaced reappearance."""
        mmsi = self._new_mmsi()
        self._vessel(mmsi, "tanker", f"DARK {self._n}")
        lat, lon = self.spec.event_center
        heading = self.spec.lane_heading_deg
        ts = self.start
        for _ in range(12):  # normal segment
            self._emit(mmsi, ts, lat, lon, speed)
            dist = speed * KN_TO_KMH * (self.interval.total_seconds() / 3600.0)
            lat, lon = _step(lat, lon, heading, dist)
            ts += self.interval
        gap_start, gap_lat, gap_lon = ts, lat, lon
        # Silence: advance time + position, emit nothing.
        silent = timedelta(minutes=gap_minutes)
        moved_km = speed * KN_TO_KMH * (silent.total_seconds() / 3600.0)
        lat, lon = _step(lat, lon, heading, moved_km)
        ts = gap_start + silent
        for _ in range(12):  # reappearance segment
            self._emit(mmsi, ts, lat, lon, speed)
            dist = speed * KN_TO_KMH * (self.interval.total_seconds() / 3600.0)
            lat, lon = _step(lat, lon, heading, dist)
            ts += self.interval
        self._set_seen(mmsi)
        self.truth.append(
            GroundTruthEvent(
                mmsi,
                "gap",
                gap_start,
                gap_start + silent,
                gap_lat,
                gap_lon,
                meta={"gap_minutes": gap_minutes, "displacement_km": round(moved_km, 1)},
            )
        )
        return mmsi

    def rendezvous_event(self, dwell_minutes: float = 45.0) -> tuple[str, str]:
        """Two vessels converge, sit co-located and slow for a window, then separate (STS)."""
        a, b = self._new_mmsi(), self._new_mmsi()
        self._vessel(a, "tanker", f"STS-A {self._n - 1}")
        self._vessel(b, "tanker", f"STS-B {self._n}")
        clat, clon = self.spec.event_center
        ts = self.start
        # Approach from opposite sides.
        for i in range(8):
            off = 0.05 * (8 - i) / 8.0
            self._emit(a, ts, clat + off, clon - off, 8.0)
            self._emit(b, ts, clat - off, clon + off, 8.0)
            ts += self.interval
        dwell_start = ts
        n_dwell = max(1, int(dwell_minutes * 60 / self.interval.total_seconds()))
        for _ in range(n_dwell):  # co-located + slow
            self._emit(a, ts, clat + 0.0008, clon, 0.2)
            self._emit(b, ts, clat - 0.0008, clon, 0.2)
            ts += self.interval
        dwell_end = ts - self.interval  # ts of the last dwell report (inclusive window)
        for i in range(8):  # separate
            off = 0.05 * (i + 1) / 8.0
            self._emit(a, ts, clat + off, clon - off, 8.0)
            self._emit(b, ts, clat - off, clon + off, 8.0)
            ts += self.interval
        self._set_seen(a)
        self._set_seen(b)
        self.truth.append(
            GroundTruthEvent(
                a, "rendezvous", dwell_start, dwell_end, clat, clon, counterpart_mmsi=b
            )
        )
        self.truth.append(
            GroundTruthEvent(
                b, "rendezvous", dwell_start, dwell_end, clat, clon, counterpart_mmsi=a
            )
        )
        return a, b

    def loiter_event(self, dwell_minutes: float = 90.0) -> str:
        """A vessel dwelling within a small radius at low speed (loitering / zone incursion)."""
        mmsi = self._new_mmsi()
        self._vessel(mmsi, "fishing", f"LOITER {self._n}")
        clat, clon = self.spec.event_center
        ts = self.start
        start = ts
        n = max(1, int(dwell_minutes * 60 / self.interval.total_seconds()))
        for k in range(n):  # small circle
            ang = 2 * math.pi * k / max(1, n)
            self._emit(mmsi, ts, clat + 0.005 * math.cos(ang), clon + 0.005 * math.sin(ang), 1.2)
            ts += self.interval
        self._set_seen(mmsi)
        self.truth.append(GroundTruthEvent(mmsi, "loiter", start, ts, clat, clon))
        return mmsi

    def spoof_event(self, speed: float = 12.0) -> str:
        """A normal track with one report teleported far away → impossible implied speed."""
        mmsi = self._new_mmsi()
        self._vessel(mmsi, "cargo", f"SPOOF {self._n}")
        lat, lon = self.spec.center
        heading = self.spec.lane_heading_deg
        ts = self.start
        for i in range(20):
            if i == 10:
                # teleport ~250 km away for a single report, then snap back next step
                jlat, jlon = _step(lat, lon, heading + 90.0, 250.0)
                self._emit(mmsi, ts, jlat, jlon, speed)
                self.truth.append(
                    GroundTruthEvent(mmsi, "spoof", ts, ts, jlat, jlon, meta={"jump_km": 250})
                )
            else:
                self._emit(mmsi, ts, lat, lon, speed)
                dist = speed * KN_TO_KMH * (self.interval.total_seconds() / 3600.0)
                lat, lon = _step(lat, lon, heading, dist)
            ts += self.interval
        self._set_seen(mmsi)
        return mmsi

    def anomaly_event(self, speed: float = 10.0) -> str:
        """A zig-zagging route unlike the straight lane — a trajectory anomaly."""
        mmsi = self._new_mmsi()
        self._vessel(mmsi, "cargo", f"ANOMALY {self._n}")
        lat, lon = self.spec.center
        ts = self.start
        start = ts
        heading = self.spec.lane_heading_deg
        for i in range(40):
            self._emit(mmsi, ts, lat, lon, speed)
            heading += 55.0 if i % 2 == 0 else -50.0  # sharp alternating turns
            dist = speed * KN_TO_KMH * (self.interval.total_seconds() / 3600.0)
            lat, lon = _step(lat, lon, heading, dist)
            ts += self.interval
        self._set_seen(mmsi)
        self.truth.append(
            GroundTruthEvent(mmsi, "anomaly", start, ts, self.spec.center[0], self.spec.center[1])
        )
        return mmsi

    def coverage_gap_trap(self, silence_minutes: float = 200.0, speed: float = 11.0) -> str:
        """A calibration TRAP: a benign vessel that loses AIS reception while barely moving.

        It transits, goes silent for a long stretch inside a sensitive zone, then reappears at
        essentially the SAME position (it anchored / drifted; it did not run dark across the
        strait). The gap detector must NOT flag it — the silence is long, but the displacement
        is ~0, which is the coverage-artifact signature, not evasion. Measures the honest limit.
        """
        mmsi = self._new_mmsi()
        self._vessel(mmsi, "cargo", f"COVERAGE-TRAP {self._n}")
        lat, lon = self.spec.event_center
        heading = self.spec.lane_heading_deg
        ts = self.start
        for _ in range(10):  # normal transit into the zone
            self._emit(mmsi, ts, lat, lon, speed)
            dist = speed * KN_TO_KMH * (self.interval.total_seconds() / 3600.0)
            lat, lon = _step(lat, lon, heading, dist)
            ts += self.interval
        gap_start = ts
        # Long silence, but it barely drifts — reappears essentially where it went quiet.
        lat, lon = _step(lat, lon, heading + 30.0, 0.3)  # <1 km drift
        ts = gap_start + timedelta(minutes=silence_minutes)
        for _ in range(10):  # resume transit
            self._emit(mmsi, ts, lat, lon, speed)
            dist = speed * KN_TO_KMH * (self.interval.total_seconds() / 3600.0)
            lat, lon = _step(lat, lon, heading, dist)
            ts += self.interval
        self._set_seen(mmsi)
        self.truth.append(
            GroundTruthEvent(mmsi, "trap", gap_start, ts, lat, lon, meta={"must_not_flag": "gap"})
        )
        return mmsi


def generate_scenario(
    region: str = "singapore",
    seed: int = 0,
    n_normal: int = 12,
    *,
    interval_s: float = 300.0,
    start: datetime | None = None,
    with_events: bool = True,
) -> Scenario:
    """Build a labelled scenario: `n_normal` benign transits + one of each injected event."""
    if region not in REGIONS:
        raise ValueError(f"unknown region {region!r}; choose from {sorted(REGIONS)}")
    start = start or datetime(2023, 1, 1, 0, 0, tzinfo=UTC)
    b = _Builder(region, seed, start, interval_s)
    for _ in range(n_normal):
        b.normal_transit()
    if with_events:
        b.gap_event()
        b.rendezvous_event()
        b.loiter_event()
        b.spoof_event()
        b.anomaly_event()
        b.coverage_gap_trap()  # a benign coverage gap the gap detector must NOT flag
    return Scenario(region=region, vessels=b.vessels, positions=b.positions, truth=b.truth)
