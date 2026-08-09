"""The composite ensemble must fuse a vessel's incidents into one transparent threat rollup and
rank corroborated, sensitive-zone threats above lone low-reliability hits."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from pharos.db.models import Incident, Vessel
from pharos.detect.ensemble import fuse_incidents, run_all, vessel_rollups
from pharos.ingest.persist import persist_scenario_or_positions
from pharos.ingest.reference import seed_zones
from pharos.ingest.synthetic import generate_scenario
from pharos.tracks.build import build_tracks


def _inc(**kw: object) -> Incident:
    base = dict(
        incident_id=kw["incident_id"],
        mmsi="1",
        detector="gap",
        incident_type="x",
        score=0.5,
        severity="low",
        reliability="C",
        ts_start=datetime(2023, 1, 1, tzinfo=UTC),
    )
    base.update(kw)
    return Incident(**base)  # type: ignore[arg-type]


def test_fuse_rewards_corroboration() -> None:
    # Two distinct detectors on the same vessel outrank a single detector at the same score.
    multi = fuse_incidents(
        "1",
        [
            _inc(incident_id="a", detector="gap", score=0.6),
            _inc(incident_id="b", detector="rendezvous", score=0.6, counterpart_mmsi="2"),
        ],
        None,
    )
    single = fuse_incidents("1", [_inc(incident_id="c", detector="gap", score=0.6)], None)
    assert multi.risk > single.risk
    assert set(multi.detectors) == {"gap", "rendezvous"}
    assert multi.counterparts == ["2"]
    assert multi.components["detector_count"] == 2


def test_fuse_exposes_components_and_reliability() -> None:
    t = fuse_incidents(
        "1",
        [
            _inc(incident_id="a", detector="spoof", score=0.9, reliability="B"),
            _inc(incident_id="b", detector="gap", score=0.5, reliability="D"),
        ],
        Vessel(mmsi="1", name="X", ship_type="tanker", flag="SG"),
    )
    assert t.reliability == "B"  # best grade wins
    assert set(t.components) == {
        "max_score",
        "detector_count",
        "diversity",
        "corroboration_factor",
        "best_reliability",
        "reliability_weight",
        "reliability_factor",
        "sensitive_zone",
        "sensitive_bonus",
    }
    assert t.name == "X" and t.flag == "SG"
    assert set(t.detectors) == {"spoof", "gap"}


def test_published_components_reconstruct_the_headline_risk() -> None:
    """The breakdown must add up, or a reviewer cannot challenge the ranking.

    Only the raw inputs used to be published, so the panel's
    "severity x corroboration x reliability" invited multiplying the numbers on screen —
    max_score x diversity x a letter grade — which lands nowhere near the headline.
    """
    for incidents in (
        [_inc(incident_id="a", detector="spoof", score=0.9, reliability="B")],
        [
            _inc(incident_id="a", detector="spoof", score=1.0, reliability="B"),
            _inc(incident_id="b", detector="gap", score=0.5, reliability="D"),
        ],
        [
            _inc(incident_id=f"i{k}", detector=d, score=1.0, reliability="A")
            for k, d in enumerate(("spoof", "gap", "loiter", "rendezvous"))
        ],
    ):
        t = fuse_incidents("1", incidents, None)
        c = t.components
        rebuilt = (
            c["max_score"] * c["corroboration_factor"] * c["reliability_factor"]
            + c["sensitive_bonus"]
        )
        assert round(rebuilt, 4) == t.risk, f"{rebuilt} != {t.risk} for {c}"


def test_end_to_end_rollups(session: Session, tmp_path) -> None:  # type: ignore[no-untyped-def]
    sc = generate_scenario("singapore", seed=0, n_normal=12)
    seed_zones(session)
    persist_scenario_or_positions(session, sc.vessels, sc.positions)
    session.commit()
    build_tracks(session, region="singapore")
    session.commit()
    stats = run_all(session, region="singapore", model_path=tmp_path / "gru.pt")
    session.commit()
    assert stats["deterministic"]["total"] >= 4

    rollups = vessel_rollups(session, region="singapore")
    assert rollups
    # Riskiest first; the top threat should carry real evidence and sit in the strait.
    top = rollups[0]
    assert top.risk == max(r.risk for r in rollups)
    assert top.incident_count >= 1
    # Every detector type that fired shows up somewhere in the rollups.
    fired = {d for r in rollups for d in r.detectors}
    assert {"gap", "rendezvous", "loiter", "spoof"} <= fired
