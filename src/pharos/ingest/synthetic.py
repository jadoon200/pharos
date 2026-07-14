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
    def __init__(
        self, region: str, seed: int, start: datetime, interval_s: float, noise_km: float = 0.03
    ) -> None:
        self.region = region
        self.spec = REGIONS[region]
        self.rng = np.random.default_rng(seed)
        self.start = start
        self.interval = timedelta(seconds=interval_s)
        # GPS/report jitter added to every position — real AIS is noisy, and without it the
        # anomaly detection is trivially separable (perfectly straight lines).
        self.noise_km = noise_km
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

    def _emit(
        self, mmsi: str, ts: datetime, lat: float, lon: float, sog: float, *, noisy: bool = True
    ) -> None:
        if noisy and self.noise_km > 0:
            lat = lat + float(self.rng.normal(0, self.noise_km / KM_PER_DEG_LAT))
            lon = lon + float(
                self.rng.normal(0, self.noise_km / (KM_PER_DEG_LAT * math.cos(math.radians(lat))))
            )
            sog = max(0.0, sog + float(self.rng.normal(0, 0.3)))
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
        """A benign lane transit — gently curving (a real lane isn't a ruler), for training."""
        mmsi = self._new_mmsi()
        self._vessel(mmsi, "cargo", f"TRANSIT {self._n}")
        # Start offset around the region center, jitter the exact lane.
        lat, lon = self.spec.center
        lat += float(self.rng.normal(0, 0.02))
        lon += float(self.rng.normal(0, 0.05))
        heading = self.spec.lane_heading_deg + float(self.rng.normal(0, 6))
        # Slow heading random-walk → gentle, realistic lane curvature (not a straight line).
        drift = float(self.rng.normal(0, 0.4))
        ts = self.start + timedelta(minutes=float(self.rng.integers(0, 30)))
        for _ in range(steps):
            self._emit(mmsi, ts, lat, lon, speed + float(self.rng.normal(0, 0.5)))
            dist = speed * KN_TO_KMH * (self.interval.total_seconds() / 3600.0)
            lat, lon = _step(lat, lon, heading, dist)
            heading += drift + float(self.rng.normal(0, 0.8))  # meander
            drift *= 0.9
            ts += self.interval
        self._set_seen(mmsi)
        self.truth.append(GroundTruthEvent(mmsi, "normal", self.positions[-1].ts, ts, lat, lon))
        return mmsi

    def benign_maneuver(self, speed: float = 12.0) -> str:
        """A LEGITIMATE manoeuvre — a single sustained course change, OR a gentle deviate-and-return
        reroute (avoiding weather/traffic). Labelled 'normal': the anomaly model's HARD NEGATIVE.

        The reroute case shares the deviate-then-rejoin *shape* of a covert detour, only gentler on
        average — so the subtlest anomalies overlap with legitimate manoeuvring and the AUC stays
        (context, not shape alone, ultimately separates them; PHAROS has only shape)."""
        mmsi = self._new_mmsi()
        self._vessel(mmsi, "cargo", f"MANEUVER {self._n}")
        lat, lon = self.spec.center
        lat += float(self.rng.normal(0, 0.02))
        lon += float(self.rng.normal(0, 0.05))
        heading = self.spec.lane_heading_deg + float(self.rng.normal(0, 6))
        ts = self.start
        reroute = bool(self.rng.random() < 0.5)
        turn = float(self.rng.choice([-1, 1])) * float(self.rng.uniform(30, 50))
        bend = float(self.rng.uniform(4.0, 6.5))  # gentle deviate-return, < the anomaly's bend
        for i in range(40):
            self._emit(mmsi, ts, lat, lon, speed + float(self.rng.normal(0, 0.5)))
            if reroute:
                if 14 <= i < 21:
                    heading += bend
                elif 21 <= i < 28:
                    heading -= bend
            elif i == 20:  # one clean sustained turn
                heading += turn
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

    def anomaly_event(self, speed: float = 12.0, magnitude: float = 1.0) -> str:
        """A SUBTLE trajectory anomaly — a smooth detour/loop off the lane, then a rejoin.

        Deliberately not a wild zig-zag: it deviates and comes back (a survey box, a covert
        diversion), which overlaps in shape space with a legitimate manoeuvre (`benign_maneuver`)
        so the model faces real ambiguity and the AUC is honest, not a trivial 1.0. `magnitude`
        scales how far it strays (used to build a spectrum of easy→hard anomalies).
        """
        mmsi = self._new_mmsi()
        self._vessel(mmsi, "cargo", f"ANOMALY {self._n}")
        lat, lon = self.spec.center
        lat += float(self.rng.normal(0, 0.02))
        lon += float(self.rng.normal(0, 0.05))
        ts = self.start
        start = ts
        base = self.spec.lane_heading_deg + float(self.rng.normal(0, 6))
        heading = base
        # A smooth excursion: bend away over the middle third, then bend back (a rounded loop).
        turn = 9.0 * magnitude
        for i in range(42):
            self._emit(mmsi, ts, lat, lon, speed + float(self.rng.normal(0, 0.5)))
            if 12 <= i < 21:
                heading += turn  # bend off the lane
            elif 21 <= i < 30:
                heading -= turn  # bend back toward it
            dist = speed * KN_TO_KMH * (self.interval.total_seconds() / 3600.0)
            lat, lon = _step(lat, lon, heading, dist)
            ts += self.interval
        self._set_seen(mmsi)
        self.truth.append(
            GroundTruthEvent(mmsi, "anomaly", start, ts, self.spec.center[0], self.spec.center[1])
        )
        return mmsi

    def benign_anchorage(self, dwell_minutes: float = 120.0, speed: float = 0.3) -> str:
        """A vessel legitimately anchored inside a PORT anchorage — must NOT be flagged as
        loitering-with-intent. A HARD NEGATIVE for the loiter detector (zone-aware: anchoring in a
        designated anchorage is expected)."""
        mmsi = self._new_mmsi()
        self._vessel(mmsi, "cargo", f"ANCHORED {self._n}")
        # Singapore Port Anchorages centre (inside the 'singapore-anchorages' port zone).
        clat, clon = (1.23, 103.85) if self.region == "singapore" else (33.68, -118.18)
        ts = self.start
        start = ts
        n = max(1, int(dwell_minutes * 60 / self.interval.total_seconds()))
        for _ in range(n):  # swinging gently at anchor
            r = float(self.rng.uniform(0, 0.003))
            ang = float(self.rng.uniform(0, 2 * math.pi))
            self._emit(mmsi, ts, clat + r * math.cos(ang), clon + r * math.sin(ang), speed)
            ts += self.interval
        self._set_seen(mmsi)
        self.truth.append(GroundTruthEvent(mmsi, "benign_anchorage", start, ts, clat, clon))
        return mmsi

    def benign_slow_pass(self) -> tuple[str, str]:
        """Two vessels pass close and slow-ish but only briefly (< the rendezvous window) — a HARD
        NEGATIVE for the STS detector, which must require a sustained co-located dwell."""
        a, b = self._new_mmsi(), self._new_mmsi()
        self._vessel(a, "cargo", f"PASS-A {self._n - 1}")
        self._vessel(b, "cargo", f"PASS-B {self._n}")
        clat, clon = self.spec.center
        ts = self.start
        start = ts
        for i in range(16):  # cross paths, momentarily near, never a sustained slow dwell
            self._emit(a, ts, clat + 0.03 * (8 - i) / 8, clon, 6.0)
            self._emit(b, ts, clat, clon + 0.03 * (8 - i) / 8, 6.0)
            ts += self.interval
        self._set_seen(a)
        self._set_seen(b)
        self.truth.append(
            GroundTruthEvent(a, "benign_pass", start, ts, clat, clon, counterpart_mmsi=b)
        )
        self.truth.append(
            GroundTruthEvent(b, "benign_pass", start, ts, clat, clon, counterpart_mmsi=a)
        )
        return a, b

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
    noise_km: float = 0.03,
) -> Scenario:
    """Build a realistic, labelled scenario for training + evaluation.

    Composition (all noisy, gently-curving tracks — no rulers):
    - `n_normal` benign lane transits (the pattern-of-life the anomaly model trains on);
    - `n_normal // 4` **benign course-changes** — legitimate single manoeuvres, hard negatives
      that must NOT rank with the anomalies;
    - graded **subtle anomalies** (detours of increasing magnitude) — the anomaly positives;
    - one of each deterministic event (gap / rendezvous / loiter / spoof);
    - **confounders that must not be flagged**: a coverage-gap trap, a port-anchorage dwell, and a
      brief slow pass. These give the deterministic detectors realistic false-alarm pressure.
    """
    if region not in REGIONS:
        raise ValueError(f"unknown region {region!r}; choose from {sorted(REGIONS)}")
    start = start or datetime(2023, 1, 1, 0, 0, tzinfo=UTC)
    b = _Builder(region, seed, start, interval_s, noise_km=noise_km)
    for _ in range(n_normal):
        b.normal_transit()
    for _ in range(max(2, n_normal // 3)):
        b.benign_maneuver()  # legitimate turns + reroutes — the anomaly model's hard negatives
    if with_events:
        b.gap_event()
        b.rendezvous_event()
        b.loiter_event()
        b.spoof_event()
        # A spectrum from obvious anomalies down to subtle detours that overlap benign reroutes,
        # so recall degrades on the subtle end (see docs/EVAL.md on the synthetic-eval ceiling).
        for mag in (1.5, 1.1, 0.8, 0.55, 0.45):
            b.anomaly_event(magnitude=mag)
        # Confounders — benign behaviours that superficially resemble threats.
        b.coverage_gap_trap()  # a benign coverage gap the gap detector must NOT flag
        b.benign_anchorage()  # legitimate anchoring in a port — the loiter detector's negative
        b.benign_slow_pass()  # a brief close pass — the STS detector's negative
    return Scenario(region=region, vessels=b.vessels, positions=b.positions, truth=b.truth)
