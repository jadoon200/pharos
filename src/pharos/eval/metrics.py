"""Eval metrics — per-detector precision/recall, the calibration trap, and anomaly AUC.

An incident *matches* a ground-truth event when it is the right detector, on the same vessel, with
overlapping time windows (a small tolerance absorbs the report cadence). From matches we get
per-detector precision/recall; the trap check counts benign coverage gaps the gap detector wrongly
flagged; the anomaly model is scored threshold-free by AUC of the injected route-anomaly against
benign transits, both within-region and cross-region (the headline).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
from numpy.typing import NDArray

from pharos.db.models import Incident, Position
from pharos.ingest.synthetic import GroundTruthEvent, Scenario
from pharos.tracks.build import segment, track_features, track_sequence

# detector name → the ground-truth event type it should find.
DETECTOR_TRUTH = {
    "gap": "gap",
    "rendezvous": "rendezvous",
    "loiter": "loiter",
    "spoof": "spoof",
}


@dataclass
class PRResult:
    detector: str
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 1.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 1.0


def _overlaps(a0: datetime, a1: datetime, b0: datetime, b1: datetime, tol: timedelta) -> bool:
    return max(a0, b0) <= min(a1, b1) + tol


def score_detector(
    detector: str,
    truth: list[GroundTruthEvent],
    incidents: list[Incident],
    tol_minutes: float = 30.0,
) -> PRResult:
    """Match one detector's incidents against its ground-truth events → precision/recall."""
    truth_type = DETECTOR_TRUTH[detector]
    events = [e for e in truth if e.event_type == truth_type]
    dets = [i for i in incidents if i.detector == detector]
    tol = timedelta(minutes=tol_minutes)

    matched_events: set[int] = set()
    matched_incidents: set[int] = set()
    for ei, e in enumerate(events):
        for di, inc in enumerate(dets):
            if inc.mmsi != e.mmsi:
                continue
            if _overlaps(inc.ts_start, inc.ts_end or inc.ts_start, e.ts_start, e.ts_end, tol):
                matched_events.add(ei)
                matched_incidents.add(di)
    tp = len(matched_events)
    fn = len(events) - tp
    fp = len(dets) - len(matched_incidents)  # incidents matching no truth event = false alarms
    return PRResult(detector=detector, tp=tp, fp=fp, fn=fn)


def trap_breaches(truth: list[GroundTruthEvent], incidents: list[Incident]) -> int:
    """Count gap incidents raised on a benign coverage-gap TRAP vessel (must be zero)."""
    trap_mmsis = {e.mmsi for e in truth if e.event_type == "trap"}
    return sum(1 for i in incidents if i.detector == "gap" and i.mmsi in trap_mmsis)


def scenario_features(
    scenario: Scenario, seq_len: int, gap_minutes: float
) -> tuple[NDArray[np.float64], list[str]]:
    """Per-voyage feature matrix + event label for a scenario (for anomaly AUC)."""
    by_vessel: dict[str, list[Position]] = {}
    for p in scenario.positions:
        by_vessel.setdefault(p.mmsi, []).append(p)
    truth = {e.mmsi: e.event_type for e in scenario.truth}
    feats: list[list[float]] = []
    labels: list[str] = []
    for mmsi, pts in by_vessel.items():
        for voyage in segment(pts, gap_minutes):
            if len(voyage) >= 6:
                feats.append(track_features(sorted(voyage, key=lambda p: p.ts), seq_len))
                labels.append(truth.get(mmsi, "normal"))
    return np.array(feats, dtype=np.float64), labels


def scenario_sequences(
    scenario: Scenario, seq_len: int, gap_minutes: float
) -> tuple[NDArray[np.float64], list[str]]:
    """Per-voyage *sequence* tensor (N, seq_len-1, 4) + event label, for the GRU model."""
    by_vessel: dict[str, list[Position]] = {}
    for p in scenario.positions:
        by_vessel.setdefault(p.mmsi, []).append(p)
    truth = {e.mmsi: e.event_type for e in scenario.truth}
    seqs: list[NDArray[np.float64]] = []
    labels: list[str] = []
    for mmsi, pts in by_vessel.items():
        for voyage in segment(pts, gap_minutes):
            if len(voyage) >= 6:
                seqs.append(track_sequence(sorted(voyage, key=lambda p: p.ts), seq_len))
                labels.append(truth.get(mmsi, "normal"))
    return np.array(seqs, dtype=np.float64), labels


def roc_auc_anomaly_vs_normal(scores: NDArray[np.float64], labels: list[str]) -> float:
    """Rank-based ROC-AUC of the route-anomaly against benign transits only (threshold-free)."""
    lab = np.array(labels)
    pos = scores[lab == "anomaly"]
    neg = scores[lab == "normal"]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return float(wins / (len(pos) * len(neg)))
