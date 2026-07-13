"""Each detector must fire on its injected synthetic event and stay quiet on clean transits."""

from __future__ import annotations

from pharos.config import get_settings
from pharos.detect.base import grade_from_confidence, severity_from_score
from pharos.detect.gaps import detect_gaps
from pharos.detect.loiter import detect_loitering
from pharos.detect.rendezvous import detect_rendezvous
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
    assert severity_from_score(0.9) == "critical"
    assert severity_from_score(0.5, sensitive_zone=True) == "high"  # bumped one level
