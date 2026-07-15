"""The eval must score the detectors correctly against ground truth, hold the calibration trap,
and produce a cross-region anomaly AUC — the honest-evaluation discipline in code."""

from __future__ import annotations

from pharos.config import get_settings
from pharos.detect.run import run_detectors
from pharos.eval.gfw_check import corroborate
from pharos.eval.metrics import (
    roc_auc_anomaly_vs_normal,
    scenario_sequences,
    score_detector,
    trap_breaches,
)
from pharos.eval.run import evaluate, render_markdown
from pharos.ingest.gfw import GfwEvent
from pharos.ingest.synthetic import generate_scenario


def test_score_detector_matches_truth() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=8)
    incidents = run_detectors(sc.positions, get_settings())
    for detector in ("gap", "rendezvous", "loiter", "spoof"):
        r = score_detector(detector, sc.truth, incidents)
        assert r.recall == 1.0, f"{detector} missed its injected event"
        assert r.precision >= 0.5


def test_spoof_is_perfect() -> None:
    sc = generate_scenario("singapore", seed=1, n_normal=8)
    incidents = run_detectors(sc.positions, get_settings())
    r = score_detector("spoof", sc.truth, incidents)
    assert r.precision == 1.0 and r.recall == 1.0  # deterministic ground truth


def test_coverage_trap_is_not_flagged() -> None:
    # The gap detector must NOT call the benign anchored-vessel coverage gap a dark ship.
    sc = generate_scenario("singapore", seed=0, n_normal=8)
    incidents = run_detectors(sc.positions, get_settings())
    assert trap_breaches(sc.truth, incidents) == 0


def test_cross_region_anomaly_auc() -> None:
    settings = get_settings()
    sg = generate_scenario("singapore", seed=0, n_normal=14)
    us = generate_scenario("us-west", seed=0, n_normal=14)
    s_sg, _ = scenario_sequences(sg, settings.anomaly_seq_len, settings.track_gap_split_minutes)
    s_us, lab_us = scenario_sequences(
        us, settings.anomaly_seq_len, settings.track_gap_split_minutes
    )
    from pharos.detect.seq_anomaly import SequenceAnomalyModel

    model = SequenceAnomalyModel(hidden=64, seed=0)
    model.fit(s_sg)  # unsupervised: train Singapore, score US west coast
    assert roc_auc_anomaly_vs_normal(model.score(s_us), lab_us) >= 0.8


def test_gfw_corroboration_counts() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=6)
    incidents = run_detectors(sc.positions, get_settings())
    gap_inc = next(i for i in incidents if i.detector == "gap")
    # SQLite can return timezone-naive datetimes even though the model declares timezone=True.
    gap_inc.ts_start = gap_inc.ts_start.replace(tzinfo=None)
    gfw = [
        GfwEvent(
            event_type="gap",
            mmsi=gap_inc.mmsi,
            start=gap_inc.ts_start,
            end=gap_inc.ts_end,
            lat=gap_inc.lat,
            lon=gap_inc.lon,
            raw={},
        )
    ]
    agreements = {a.detector: a for a in corroborate(incidents, gfw)}
    assert agreements["gap"].corroborated >= 1


def test_gfw_corroboration_requires_matching_vessel() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=6)
    incidents = run_detectors(sc.positions, get_settings())
    gap_inc = next(i for i in incidents if i.detector == "gap")
    mismatched = GfwEvent(
        event_type="gap",
        mmsi="unrelated-vessel",
        start=gap_inc.ts_start,
        end=gap_inc.ts_end,
        lat=gap_inc.lat,
        lon=gap_inc.lon,
        raw={},
    )
    agreements = {a.detector: a for a in corroborate(incidents, [mismatched])}
    assert agreements["gap"].corroborated == 0


def test_gfw_encounter_matches_either_vessel() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=6)
    incidents = run_detectors(sc.positions, get_settings())
    rendezvous = next(i for i in incidents if i.detector == "rendezvous")
    event = GfwEvent(
        event_type="rendezvous",
        mmsi="other-primary",
        start=rendezvous.ts_start,
        end=rendezvous.ts_end,
        lat=rendezvous.lat,
        lon=rendezvous.lon,
        raw={"encounter": {"vessel": {"ssvid": rendezvous.mmsi}}},
    )
    agreements = {a.detector: a for a in corroborate(incidents, [event])}
    assert agreements["rendezvous"].corroborated >= 1


def test_evaluate_end_to_end() -> None:
    results = evaluate()
    per = results["per_detector"]
    assert isinstance(per, dict)
    assert per["spoof"]["recall"] == 1.0
    # The flagship GRU transfers cross-region; and it beats the collapsing PCA baseline.
    assert float(results["anomaly_gru_cross_auc"]) >= 0.8  # type: ignore[arg-type]
    assert float(results["anomaly_gru_within_auc"]) > float(results["anomaly_pca_within_auc"])  # type: ignore[arg-type]
    md = render_markdown(results)
    assert "cross-region" in md.lower() and "gru" in md.lower()
