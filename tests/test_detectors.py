"""Each detector must fire on its injected synthetic event and stay quiet on clean transits."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise

from pharos.config import get_settings
from pharos.db.models import Position
from pharos.detect.base import grade_from_confidence, severity_from_score
from pharos.detect.gaps import detect_gaps
from pharos.detect.loiter import detect_loitering
from pharos.detect.rendezvous import _spatial_candidate_pairs, detect_rendezvous
from pharos.detect.run import run_detectors
from pharos.detect.spoof import detect_spoofing
from pharos.ingest.synthetic import generate_scenario


def _truth_mmsi(sc, event_type: str) -> str:  # type: ignore[no-untyped-def]
    return next(e.mmsi for e in sc.truth if e.event_type == event_type)


def test_gap_detector_fires_on_dark_ship() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=4)
    incidents = detect_gaps(sc.positions, get_settings())
    dark = _truth_mmsi(sc, "gap")
    hits = [i for i in incidents if i.mmsi == dark]
    assert hits, "gap detector missed the injected dark-ship"
    inc = hits[0]
    assert inc.detector == "gap"
    assert inc.evidence["gap_minutes"] >= 120
    assert "coverage_caveat" in inc.evidence  # honesty about the confound
    assert inc.reliability in {"C", "D", "E", "F"}  # never certified high


def test_gap_detector_suppresses_known_coverage_outage() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=4)
    dark = _truth_mmsi(sc, "gap")
    points = sorted((p for p in sc.positions if p.mmsi == dark), key=lambda p: p.ts)
    gap_pair = next(
        (left, right)
        for left, right in pairwise(points)
        if (right.ts - left.ts).total_seconds() >= 120 * 60
    )

    suppressed = detect_gaps(
        sc.positions,
        get_settings(),
        [(gap_pair[0].ts + timedelta(minutes=1), gap_pair[1].ts - timedelta(minutes=1))],
    )
    unrelated = detect_gaps(
        sc.positions,
        get_settings(),
        [(gap_pair[1].ts + timedelta(days=1), gap_pair[1].ts + timedelta(days=2))],
    )

    assert not [incident for incident in suppressed if incident.mmsi == dark]
    assert [incident for incident in unrelated if incident.mmsi == dark]


def test_rendezvous_detector_fires_on_sts() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=4)
    incidents = detect_rendezvous(sc.positions, get_settings())
    a = _truth_mmsi(sc, "rendezvous")
    hits = [i for i in incidents if i.mmsi == a]
    assert hits, "rendezvous detector missed the injected STS"
    inc = hits[0]
    assert inc.counterpart_mmsi is not None
    assert inc.evidence["min_range_km"] <= 0.5
    # It is symmetric: both vessels get an incident naming the other.
    counterpart_hits = [i for i in incidents if i.mmsi == inc.counterpart_mmsi]
    assert counterpart_hits and counterpart_hits[0].counterpart_mmsi == a


def test_rendezvous_index_matches_exhaustive_detector() -> None:
    settings = get_settings()
    for seed in range(3):
        sc = generate_scenario("singapore", seed=seed, n_normal=12)
        indexed = detect_rendezvous(sc.positions, settings)
        exhaustive = detect_rendezvous(sc.positions, settings, use_candidate_index=False)
        indexed_result = [(i.incident_id, i.evidence) for i in indexed]
        exhaustive_result = [(i.incident_id, i.evidence) for i in exhaustive]
        assert indexed_result == exhaustive_result


def test_focused_rendezvous_matches_full_for_dirty_vessel() -> None:
    settings = get_settings()
    sc = generate_scenario("singapore", seed=0, n_normal=12)
    dirty = _truth_mmsi(sc, "rendezvous")
    full = detect_rendezvous(sc.positions, settings)
    focused = detect_rendezvous(sc.positions, settings, focus_mmsis={dirty})
    expected = [
        incident
        for incident in full
        if incident.mmsi == dirty or incident.counterpart_mmsi == dirty
    ]

    assert [(item.incident_id, item.evidence) for item in focused] == [
        (item.incident_id, item.evidence) for item in expected
    ]


def test_rendezvous_index_handles_unaligned_reports_and_prunes_distance() -> None:
    start = datetime(2023, 1, 1, tzinfo=UTC)

    def _track(mmsi: str, lon: float, offset_min: int) -> list[Position]:
        samples = ((0, 4.0), (5, 0.2), (45, 0.2), (50, 4.0))
        return [
            Position(
                mmsi=mmsi,
                ts=start + timedelta(minutes=offset_min + minute),
                lat=5.0,
                lon=lon,
                sog=speed,
                source="synthetic",
            )
            for minute, speed in samples
        ]

    usable = {"a": _track("a", 100.0, 2), "b": _track("b", 100.001, 4)}
    # Add many simultaneously slow tracks far outside rendezvous range.
    for i in range(40):
        mmsi = f"far-{i:02d}"
        usable[mmsi] = _track(mmsi, 110.0 + i, i % 3)

    pairs, time_bins = _spatial_candidate_pairs(usable, max_distance_km=0.5, max_speed_kn=1.0)

    assert ("a", "b") in pairs  # pair-specific resampling phases remain represented
    assert time_bins >= 2
    assert len(pairs) < len(usable) * (len(usable) - 1) // 20


def test_loiter_detector_fires() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=4)
    incidents = detect_loitering(sc.positions, get_settings())
    loiterer = _truth_mmsi(sc, "loiter")
    hits = [i for i in incidents if i.mmsi == loiterer]
    assert hits
    assert hits[0].evidence["duration_minutes"] >= 60


def test_spoof_detector_is_certain() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=4)
    incidents = detect_spoofing(sc.positions, get_settings())
    spoofer = _truth_mmsi(sc, "spoof")
    hits = [i for i in incidents if i.mmsi == spoofer]
    assert hits, "spoof detector missed the impossible jump"
    inc = hits[0]
    assert inc.reliability == "B"  # the one near-certain signal
    assert inc.evidence["implied_speed_kn"] > get_settings().spoof_max_speed_kn


def test_normal_transits_are_quiet() -> None:
    # A scenario with only benign transits should raise no incidents.
    sc = generate_scenario("singapore", seed=3, n_normal=6, with_events=False)
    incidents = run_detectors(sc.positions, get_settings())
    assert incidents == []


def test_ensemble_covers_every_injected_type() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=6)
    incidents = run_detectors(sc.positions, get_settings())
    fired = {i.detector for i in incidents}
    assert {"gap", "rendezvous", "loiter", "spoof"} <= fired


def test_grade_and_severity_helpers() -> None:
    assert grade_from_confidence(0.95) == "B"  # never A for AIS
    assert grade_from_confidence(0.1) == "F"
    # Thresholds match the HORUS air lane; no sensitive-zone bump (that only nudges
    # reliability), so "critical" reflects the score rather than the location.
    assert severity_from_score(0.9) == "critical"
    assert severity_from_score(0.85) == "critical"
    assert severity_from_score(0.84) == "high"
    assert severity_from_score(0.65) == "high"
    assert severity_from_score(0.5) == "moderate"
    assert severity_from_score(0.3) == "low"
