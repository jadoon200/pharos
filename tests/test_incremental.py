from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pharos.config import Settings, get_settings
from pharos.db.models import Incident, Track
from pharos.detect.run import detect
from pharos.detect.seq_anomaly import SequenceAnomalyModel
from pharos.ingest.persist import persist_scenario_or_positions
from pharos.ingest.synthetic import generate_scenario
from pharos.tracks.build import build_tracks
from pharos.tracks.incremental import rebuild_dirty_incidents, score_dirty_tracks


def _incident_snapshot(session: Session, dirty_mmsi: str) -> list[tuple[object, ...]]:
    incidents = session.scalars(select(Incident).order_by(Incident.incident_id)).all()
    return [
        (
            incident.incident_id,
            incident.mmsi,
            incident.counterpart_mmsi,
            incident.detector,
            incident.score,
            incident.reliability,
            incident.ts_start,
            incident.ts_end,
            incident.evidence,
        )
        for incident in incidents
        if incident.mmsi == dirty_mmsi or incident.counterpart_mmsi == dirty_mmsi
    ]


def test_incremental_detectors_match_full_for_dirty_vessel(session: Session) -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=12)
    persist_scenario_or_positions(session, sc.vessels, sc.positions)
    session.commit()
    build_tracks(session, region="singapore")
    detect(session, region="singapore")
    session.commit()
    dirty_mmsi = next(event.mmsi for event in sc.truth if event.event_type == "rendezvous")
    expected = _incident_snapshot(session, dirty_mmsi)

    stats = rebuild_dirty_incidents(
        session,
        {dirty_mmsi},
        region="singapore",
    )
    session.commit()

    assert stats["rendezvous"] >= 2
    assert _incident_snapshot(session, dirty_mmsi) == expected


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_incremental_anomaly_uses_only_pinned_artifact(session: Session, tmp_path: Path) -> None:
    sc = generate_scenario("singapore", seed=2, n_normal=16)
    persist_scenario_or_positions(session, sc.vessels, sc.positions)
    session.commit()
    build_tracks(session, region="singapore")
    session.commit()
    tracks = [
        track
        for track in session.scalars(select(Track).where(Track.sequence.isnot(None)))
        if track.sequence
    ]
    sequences = np.array([track.sequence for track in tracks], dtype=np.float64)
    model = SequenceAnomalyModel(hidden=get_settings().anomaly_hidden, seed=0)
    model.fit(sequences)
    model.calibrate(sequences, 50.0)
    artifact = tmp_path / "frozen.pt"
    model.save(artifact, metadata={"freeze": "unit-test"})
    digest = _sha256(artifact)
    settings = Settings(_env_file=None, anomaly_model_sha256=digest)
    dirty_mmsis = {track.mmsi for track in tracks}

    stats = score_dirty_tracks(
        session,
        dirty_mmsis,
        region="singapore",
        settings=settings,
        artifact_path=artifact,
    )
    session.commit()

    assert stats["artifact_sha256"] == digest
    assert stats["scored"] == len(tracks)
    assert stats["flagged"] >= 1
    incidents = session.scalars(select(Incident).where(Incident.detector == "anomaly")).all()
    assert incidents
    assert all(incident.evidence["artifact_sha256"] == digest for incident in incidents)
    assert all(incident.evidence["model_source"] == "frozen-artifact" for incident in incidents)

    mismatched = Settings(_env_file=None, anomaly_model_sha256="0" * 64)
    with pytest.raises(ValueError, match="artifact SHA-256 mismatch"):
        score_dirty_tracks(
            session,
            dirty_mmsis,
            region="singapore",
            settings=mismatched,
            artifact_path=artifact,
        )
