"""The GEOINT bridge must shape incidents into ARGUS-compatible, source-rated evidence."""

from __future__ import annotations

from datetime import UTC, datetime

from pharos.db.models import Incident
from pharos.geoint import credibility_from_score, incident_summary, to_evidence


def _inc(**kw: object) -> Incident:
    base = dict(
        incident_id="gap-563000019-1",
        mmsi="563000019",
        detector="gap",
        incident_type="dark ship (AIS gap)",
        score=0.85,
        severity="critical",
        reliability="C",
        ts_start=datetime(2023, 1, 1, 12, 0, tzinfo=UTC),
        zone_id="singapore-strait",
        techniques=["ais-gap", "going-dark"],
        evidence={"gap_minutes": 180, "displacement_km": 33.0},
    )
    base.update(kw)
    return Incident(**base)  # type: ignore[arg-type]


def test_credibility_mapping_never_confirmed() -> None:
    assert credibility_from_score(0.99) == 2  # never 1 (confirmed) for a lone AIS signal
    assert credibility_from_score(0.7) == 3
    assert credibility_from_score(0.1) == 6


def test_evidence_has_argus_fields() -> None:
    ev = to_evidence(_inc())
    # The ARGUS EvidenceItem contract.
    for field in ("doc_id", "title", "source", "reliability", "credibility", "summary", "url"):
        assert field in ev
    assert ev["doc_id"] == "gap-563000019-1"
    assert ev["reliability"] == "C"
    assert ev["credibility"] == 2
    assert ev["source"] == "PHAROS maritime domain awareness"
    assert ev["url"] == "/incidents/gap-563000019-1"
    # Geospatial extras present too.
    assert ev["kind"] == "geoint" and ev["mmsi"] == "563000019"
    assert ev["zone"] == "Singapore Strait"


def test_summary_is_human_review() -> None:
    s = incident_summary(_inc())
    assert "Singapore Strait" in s
    assert "180" in s and "33" in s  # gap duration + displacement
    assert "not a verdict" in s.lower()
    assert "coverage loss" in s.lower()  # the honesty caveat for gaps


def test_rendezvous_summary_names_counterpart() -> None:
    s = incident_summary(
        _inc(
            detector="rendezvous",
            incident_type="ship-to-ship transfer",
            counterpart_mmsi="563000020",
            evidence={"duration_minutes": 45},
            zone_id=None,
        )
    )
    assert "563000020" in s and "45" in s
