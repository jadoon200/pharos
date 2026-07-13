"""Global Fishing Watch cross-check — validate PHAROS incidents against real-world event labels.

The synthetic gold set makes the offline eval deterministic and labelled, but the honest question
is whether the detectors fire on *real* AIS. GFW independently derives encounters (≈ rendezvous),
loitering, and gap events from real AIS; this cross-check counts how many PHAROS incidents of each
type fall near a GFW event of the corresponding type (same rough time + place).

Opt-in and non-fatal: with no `PHAROS_GFW_TOKEN` it reports "skipped" so `make eval` still runs
end-to-end offline. This is a corroboration signal, not ground truth — GFW has its own models.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from pharos.db.models import Incident
from pharos.geo import haversine_km
from pharos.ingest.gfw import GfwEvent
from pharos.logging import get_logger

log = get_logger(__name__)


@dataclass
class GfwAgreement:
    detector: str
    incidents: int
    corroborated: int  # incidents near a matching-type GFW event

    @property
    def rate(self) -> float:
        return self.corroborated / self.incidents if self.incidents else float("nan")


def corroborate(
    incidents: list[Incident],
    gfw_events: list[GfwEvent],
    match_km: float = 25.0,
    match_hours: float = 24.0,
) -> list[GfwAgreement]:
    """For each detector, count incidents near a same-type GFW event in space and time."""
    tol = timedelta(hours=match_hours)
    out: list[GfwAgreement] = []
    for detector in ("gap", "rendezvous", "loiter"):
        dets = [i for i in incidents if i.detector == detector]
        gfw = [g for g in gfw_events if g.event_type == detector]
        corroborated = 0
        for inc in dets:
            if inc.lat is None or inc.lon is None:
                continue
            for g in gfw:
                if g.lat is None or g.lon is None or g.start is None:
                    continue
                near = haversine_km(inc.lat, inc.lon, g.lat, g.lon) <= match_km
                timely = abs(inc.ts_start - g.start) <= tol
                if near and timely:
                    corroborated += 1
                    break
        out.append(GfwAgreement(detector=detector, incidents=len(dets), corroborated=corroborated))
    return out
