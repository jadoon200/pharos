"""Empirical AIS receiver-coverage model — turning the gap confound into a measurement.

The dark-ship detector's honest limitation has always been that an AIS gap is frequently a
benign *reception* loss rather than a vessel going dark deliberately. Until now that was
handled by capping confidence with a constant, and the external cross-check failed to
calibrate it: every Global Fishing Watch gap label in the Gulf corpus sits 94-209 km
offshore, outside NOAA Marine Cadastre's terrestrial footprint, so no PHAROS gap call has
ever overlapped an independent label (`docs/EVAL.md`).

This module replaces the constant with a measurement that needs **no external label at
all**, because the corpus answers the question itself: *while this vessel was silent, was
anyone else being heard where it went quiet?*

- If many other vessels were received along the corridor during the silent window, the
  receivers demonstrably worked there — the silence is attributable to the vessel.
- If nothing at all was heard there, a coverage hole is the parsimonious explanation and
  the call is downgraded rather than dressed up as evasion.

The model is a witness index: (grid cell x time bucket) -> set of MMSIs heard. Building it
is one pass over the same positions the detectors already load.

**Stated limitation (kept in the open):** the vessel's true path while dark is unknown —
that is the definition of the gap. The corridor is the great-circle interpolation between
its last and next known fixes, i.e. the most charitable straight-line assumption. A vessel
that detoured was somewhere this model did not test. Endpoint neighbourhoods, which are
observed rather than assumed, are therefore reported separately from corridor support.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from pharos.db.models import Position
from pharos.timeutil import utc_naive

# (cell_lat, cell_lon, time_bucket) -> distinct MMSIs heard in that cell during that bucket.
_WitnessKey = tuple[int, int, int]

_EPOCH = datetime(1970, 1, 1)


@dataclass(frozen=True)
class CoverageAssessment:
    """What the corpus itself says about a gap's silence."""

    corridor_samples: int
    corridor_witnessed: int  # samples whose cell heard >= min_witnesses other vessels
    witness_vessels: int  # distinct other vessels heard anywhere along the corridor
    endpoint_witnessed: int  # of the 2 observed endpoints, how many were witnessed
    verdict: str  # "vessel-attributed" | "partial" | "coverage-explained"

    @property
    def corridor_support(self) -> float:
        """Fraction of the corridor demonstrably audible while the vessel was silent."""
        if self.corridor_samples == 0:
            return 0.0
        return self.corridor_witnessed / self.corridor_samples

    def as_evidence(self) -> dict[str, object]:
        return {
            "coverage_verdict": self.verdict,
            "corridor_support": round(self.corridor_support, 3),
            "corridor_samples": self.corridor_samples,
            "corridor_witnessed": self.corridor_witnessed,
            "witness_vessels": self.witness_vessels,
            "endpoint_witnessed": self.endpoint_witnessed,
            "coverage_method": (
                "other vessels heard along the interpolated corridor during the silent window; "
                "the dark vessel's true path is unknown, so corridor support assumes a "
                "great-circle track between its last and next fixes"
            ),
        }


class CoverageModel:
    """Witness index over the corpus: who else was being heard, where, and when.

    `cell_deg` is the spatial resolution and `bucket_minutes` the temporal one. Defaults are
    coarse on purpose: the question is "were the receivers working in this neighbourhood at
    all", not "was this exact pixel covered".
    """

    def __init__(
        self,
        *,
        cell_deg: float = 0.25,
        bucket_minutes: float = 60.0,
        min_witnesses: int = 2,
        corridor_samples: int = 12,
    ) -> None:
        self.cell_deg = cell_deg
        self.bucket_minutes = bucket_minutes
        self.min_witnesses = min_witnesses
        self.corridor_samples = corridor_samples
        self._witnesses: dict[_WitnessKey, set[str]] = defaultdict(set)

    # --- construction -------------------------------------------------------------------
    def _cell(self, lat: float, lon: float) -> tuple[int, int]:
        return (int(lat // self.cell_deg), int(lon // self.cell_deg))

    def _bucket(self, ts: datetime) -> int:
        return int((utc_naive(ts) - _EPOCH).total_seconds() // (self.bucket_minutes * 60.0))

    def add(self, mmsi: str, lat: float, lon: float, ts: datetime) -> None:
        ci, cj = self._cell(lat, lon)
        self._witnesses[(ci, cj, self._bucket(ts))].add(mmsi)

    @classmethod
    def from_positions(cls, positions: list[Position], **kwargs: float | int) -> CoverageModel:
        model = cls(**kwargs)  # type: ignore[arg-type]
        for p in positions:
            model.add(p.mmsi, p.lat, p.lon, p.ts)
        return model

    # --- querying -----------------------------------------------------------------------
    def witnesses_at(
        self, lat: float, lon: float, start: datetime, end: datetime, *, exclude: str
    ) -> set[str]:
        """Distinct vessels other than `exclude` heard in this cell during [start, end]."""
        ci, cj = self._cell(lat, lon)
        found: set[str] = set()
        for bucket in range(self._bucket(start), self._bucket(end) + 1):
            found |= self._witnesses.get((ci, cj, bucket), set())
        found.discard(exclude)
        return found

    def assess_gap(
        self,
        mmsi: str,
        lat0: float,
        lon0: float,
        lat1: float,
        lon1: float,
        start: datetime,
        end: datetime,
    ) -> CoverageAssessment:
        """Measure whether the corpus can attribute this silence to the vessel."""
        n = max(2, self.corridor_samples)
        witnessed = 0
        all_witnesses: set[str] = set()
        endpoint_witnessed = 0
        for i in range(n):
            f = i / (n - 1)
            # Linear interpolation is adequate at these cell sizes; the corridor is a
            # neighbourhood test, not a navigation solution.
            lat = lat0 + (lat1 - lat0) * f
            lon = lon0 + (lon1 - lon0) * f
            seen = self.witnesses_at(lat, lon, start, end, exclude=mmsi)
            all_witnesses |= seen
            if len(seen) >= self.min_witnesses:
                witnessed += 1
                if i in (0, n - 1):
                    endpoint_witnessed += 1

        support = witnessed / n
        # Thresholds are deliberately conservative: a call is only promoted to
        # "vessel-attributed" when most of the corridor was demonstrably audible.
        if support >= 0.75:
            verdict = "vessel-attributed"
        elif support >= 0.25:
            verdict = "partial"
        else:
            verdict = "coverage-explained"
        return CoverageAssessment(
            corridor_samples=n,
            corridor_witnessed=witnessed,
            witness_vessels=len(all_witnesses),
            endpoint_witnessed=endpoint_witnessed,
            verdict=verdict,
        )


# Confidence multipliers applied to a gap incident's base confidence. A coverage-explained
# gap is not deleted — it stays visible as a low-grade lead, because the model measures
# *reception*, not intent.
CONFIDENCE_BY_VERDICT = {
    "vessel-attributed": 1.6,
    "partial": 1.0,
    "coverage-explained": 0.5,
}
