from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pharos.config import Settings
from pharos.db.models import (
    CollectorRun,
    CoverageOutage,
    EvaluationRun,
    EventTrackMatch,
    ExternalEvent,
    Incident,
    Position,
    Track,
    TrackReview,
    Vessel,
)
from pharos.eval.pilot import run_pilot_evaluation, wilson_interval
from pharos.ingest.gfw import GfwEvent
from pharos.labels.alerts import detectors_by_track, incident_track_pairs
from pharos.labels.coverage import observed_hours, observed_intervals
from pharos.labels.external import GFW_ATTRIBUTION, import_gfw_events, import_yaml_events
from pharos.labels.match import match_event
from pharos.labels.review import (
    blinded_payload,
    build_rereview_queue,
    build_review_queue,
    queue_sampling_design,
)

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def _settings(tmp_path: Path | None = None, **values: object) -> Settings:
    defaults: dict[str, object] = {
        "_env_file": None,
        "database_url": "sqlite:///:memory:",
        "pilot_start_at": NOW - timedelta(days=4),
        "collector_region": "singapore-live",
    }
    if tmp_path is not None:
        defaults["labels_dir"] = tmp_path / "labels"
        defaults["anomaly_model_dir"] = tmp_path / "models"
    defaults.update(values)
    return Settings(**defaults)


def _vessel(session: Session, mmsi: str = "563000001") -> Vessel:
    vessel = Vessel(mmsi=mmsi, name="TEST", ship_type="cargo")
    session.add(vessel)
    session.flush()
    return vessel


def _track(
    session: Session,
    *,
    mmsi: str = "563000001",
    index: int = 0,
    start: datetime | None = None,
) -> Track:
    start = start or NOW - timedelta(hours=4)
    track = Track(
        track_id=f"{mmsi}:{index:04d}",
        mmsi=mmsi,
        region="singapore-live",
        start_ts=start,
        end_ts=start + timedelta(hours=1),
        point_count=3,
        distance_km=10,
        start_lat=1.2,
        start_lon=103.8,
        end_lat=1.21,
        end_lon=103.9,
        sequence=[[1.0, 0.0, 1.0, 0.0], [1.0, 0.2, 1.02, 5.0]],
    )
    session.add(track)
    session.flush()
    return track


def test_coverage_subtracts_outages(session: Session) -> None:
    start = NOW - timedelta(hours=10)
    run = CollectorRun(
        started_at=start,
        last_message_at=NOW,
        stopped_at=NOW,
        report_count=10,
        vessel_count=1,
        status="stopped",
    )
    session.add(run)
    session.flush()
    session.add(
        CoverageOutage(
            run_id=run.id,
            opened_at=start + timedelta(hours=3),
            closed_at=start + timedelta(hours=5),
            reason="test outage",
        )
    )
    session.commit()

    intervals = observed_intervals(session, now=NOW)

    assert [(item.start.hour, item.end.hour) for item in intervals] == [(2, 5), (7, 12)]
    assert observed_hours(intervals) == 8.0


def test_yaml_import_is_strict_bounded_and_idempotent(session: Session, tmp_path: Path) -> None:
    directory = tmp_path / "labels" / "recaap"
    directory.mkdir(parents=True)
    event_file = directory / "incident.yaml"
    event_file.write_text(
        """source: recaap
source_ref: SS-2026-041
source_url: https://www.recaap.org/example
event_type: incident-robbery
occurred_start: 2026-07-18T19:40:00Z
occurred_end: null
lat: 1.174
lon: 103.867
mmsi: null
imo: '9876543'
vessel_name: EXAMPLE STAR
source_confidence: official
retrieved: 2026-07-20
notes: boarded at anchor; minimal facts only
""",
        encoding="utf-8",
    )

    assert import_yaml_events(session, tmp_path / "labels") == {"recaap": 1, "tsib": 0}
    assert import_yaml_events(session, tmp_path / "labels") == {"recaap": 1, "tsib": 0}
    assert session.scalar(select(func.count()).select_from(ExternalEvent)) == 1
    stored = session.get(ExternalEvent, "recaap:SS-2026-041")
    assert stored is not None and stored.raw == {"notes": "boarded at anchor; minimal facts only"}

    event_file.write_text(event_file.read_text() + "unknown: rejected\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        import_yaml_events(session, tmp_path / "labels")
    event_file.write_text(
        event_file.read_text().replace("unknown: rejected\n", ""), encoding="utf-8"
    )
    event_file.write_text(
        event_file.read_text().replace("boarded at anchor; minimal facts only", "x" * 301)
    )
    with pytest.raises(ValidationError):
        import_yaml_events(session, tmp_path / "labels")


def test_gfw_import_is_attributed_and_idempotent(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_fetch(
        event_type: str,
        _start: str,
        _end: str,
        _bbox: tuple[float, float, float, float],
    ) -> list[GfwEvent]:
        if event_type != "rendezvous":
            return []
        return [
            GfwEvent(
                event_type="rendezvous",
                mmsi="563000001",
                start=NOW - timedelta(hours=2),
                end=NOW - timedelta(hours=1),
                lat=1.2,
                lon=103.8,
                raw={"id": "gfw-1", "type": "encounter"},
                event_id="gfw-1",
            )
        ]

    monkeypatch.setattr("pharos.labels.external.fetch_events", fake_fetch)
    settings = _settings()
    import_gfw_events(session, settings, now=NOW)
    import_gfw_events(session, settings, now=NOW + timedelta(hours=1))

    assert session.scalar(select(func.count()).select_from(ExternalEvent)) == 1
    event = session.get(ExternalEvent, "gfw-encounter:gfw-1")
    assert event is not None
    assert event.attribution == GFW_ATTRIBUTION
    assert event.retrieved_at.replace(tzinfo=UTC) == NOW + timedelta(hours=1)


def test_matcher_is_deterministic_and_records_all_terminal_states(session: Session) -> None:
    vessel = _vessel(session)
    start = NOW - timedelta(hours=5)
    track = _track(session, start=start)
    session.add_all(
        [
            Position(
                mmsi=vessel.mmsi,
                ts=start + timedelta(minutes=10),
                lat=1.2,
                lon=103.8,
                source="aisstream",
                region="singapore-live",
            ),
            CollectorRun(
                started_at=NOW - timedelta(days=1),
                last_message_at=NOW,
                stopped_at=NOW,
                report_count=1,
                vessel_count=1,
                status="stopped",
            ),
        ]
    )
    matched = ExternalEvent(
        event_id="recaap:match",
        source="recaap",
        source_ref="match",
        source_url="https://example.com/match",
        event_type="incident-robbery",
        mmsi=vessel.mmsi,
        ts_start=start + timedelta(minutes=20),
        lat=1.2,
        lon=103.8,
        source_confidence="official",
        retrieved_at=NOW,
        attribution="Source: test.",
    )
    unmatched = ExternalEvent(
        event_id="recaap:unmatched",
        source="recaap",
        source_ref="unmatched",
        source_url="https://example.com/unmatched",
        event_type="incident-other",
        ts_start=NOW - timedelta(hours=2),
        lat=-20,
        lon=-20,
        source_confidence="official",
        retrieved_at=NOW,
        attribution="Source: test.",
    )
    outside = ExternalEvent(
        event_id="recaap:outside",
        source="recaap",
        source_ref="outside",
        source_url="https://example.com/outside",
        event_type="incident-other",
        ts_start=NOW + timedelta(days=2),
        lat=1.2,
        lon=103.8,
        source_confidence="official",
        retrieved_at=NOW,
        attribution="Source: test.",
    )
    session.add_all([matched, unmatched, outside])
    session.commit()
    settings = _settings()

    first = match_event(session, matched, settings, now=NOW)
    second = match_event(session, matched, settings, now=NOW)
    no_candidate = match_event(session, unmatched, settings, now=NOW)
    no_coverage = match_event(session, outside, settings, now=NOW)

    assert [row.match_id for row in first] == [row.match_id for row in second]
    assert second[0].track_id == track.track_id and second[0].identifier_match
    assert no_candidate[0].status == "unmatched" and no_candidate[0].in_observed_coverage
    assert no_coverage[0].status == "unmatched" and not no_coverage[0].in_observed_coverage


def test_review_queue_reproducible_blinded_and_rereviewed(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vessel = _vessel(session)
    tracks = [_track(session, index=index) for index in range(230)]
    for index, track in enumerate(tracks[:60]):
        session.add(
            Incident(
                incident_id=f"incident-{index}",
                mmsi=vessel.mmsi,
                track_id=track.track_id,
                detector="spoof" if index == 0 else "anomaly",
                incident_type="candidate",
                score=1 - index / 100,
                severity="moderate",
                reliability="C",
                ts_start=track.start_ts,
                region="singapore-live",
                evidence={"implied_speed_kn": 80} if index == 0 else {},
            )
        )
    event = ExternalEvent(
        event_id="recaap:queue",
        source="recaap",
        source_ref="queue",
        source_url="https://example.com/queue",
        event_type="incident-other",
        ts_start=NOW,
        source_confidence="official",
        retrieved_at=NOW,
        attribution="Source: test.",
    )
    session.add(event)
    session.flush()
    session.add(
        EventTrackMatch(
            match_id=f"{event.event_id}:{tracks[0].track_id}",
            event_id=event.event_id,
            track_id=tracks[0].track_id,
            identifier_match=True,
            rule_version="match-v1",
            status="matched",
            in_observed_coverage=True,
        )
    )
    session.commit()
    monkeypatch.setattr("pharos.labels.review._near_threshold_ids", lambda *_args: set())
    settings = _settings(tmp_path)

    first_design = build_review_queue(session, settings)
    first_order = session.scalars(
        select(TrackReview.review_id).order_by(TrackReview.queue_order)
    ).all()
    second_design = build_review_queue(session, settings)

    assert len(first_order) == 220
    assert first_design == second_design
    # Frozen at build time: tracks accruing after the draw must not skew the recorded
    # inclusion probabilities that inverse-probability weighting relies on.
    _track(session, mmsi="563009999", index=99)
    session.commit()
    assert queue_sampling_design(session, settings) == first_design
    assert (
        first_order
        == session.scalars(select(TrackReview.review_id).order_by(TrackReview.queue_order)).all()
    )
    review = session.get(TrackReview, first_order[0])
    assert review is not None
    payload_text = json.dumps(blinded_payload(review, session.get(Track, review.track_id)))
    for hidden in (vessel.mmsi, review.track_id, review.stratum, "detector", "score", "latitude"):
        assert hidden not in payload_text

    primaries = session.scalars(
        select(TrackReview).where(TrackReview.reviewer_id == "primary")
    ).all()
    for primary in primaries[:20]:
        primary.label = "normal"
        primary.reviewed_at = NOW - timedelta(days=4)
    session.commit()
    count = build_rereview_queue(session, settings, now=NOW)
    assert count == 3
    assert (
        session.scalar(
            select(func.count())
            .select_from(TrackReview)
            .where(TrackReview.reviewer_id == "primary-rereview")
        )
        == 3
    )


def test_deterministic_incidents_attribute_to_containing_tracks(session: Session) -> None:
    """Gap/loiter/rendezvous incidents carry no track_id; attribution must still find their
    track so the pharos-alert stratum and alert-precision denominators are never empty."""
    vessel = _vessel(session)
    track = _track(session)
    session.add(
        Incident(
            incident_id="det-incident-0",
            mmsi=vessel.mmsi,
            track_id=None,
            detector="gap",
            incident_type="dark ship (AIS gap)",
            score=0.7,
            severity="moderate",
            reliability="C",
            ts_start=track.start_ts + timedelta(minutes=10),
            ts_end=track.start_ts + timedelta(minutes=40),
            region="singapore-live",
            evidence={},
        )
    )
    session.commit()
    pairs = incident_track_pairs(session, "singapore-live")
    assert [(incident.detector, track_id) for incident, track_id in pairs] == [
        ("gap", track.track_id)
    ]
    assert detectors_by_track(pairs) == {track.track_id: {"gap"}}


def test_wilson_and_evaluation_are_reproducible(session: Session, tmp_path: Path) -> None:
    interval = wilson_interval(1, 2)
    assert interval["ci95"] == [0.094531, 0.905469]
    vessel = _vessel(session)
    track = _track(session)
    session.add(
        Incident(
            incident_id="eval-incident",
            mmsi=vessel.mmsi,
            track_id=track.track_id,
            detector="anomaly",
            incident_type="trajectory anomaly",
            score=0.9,
            severity="high",
            reliability="C",
            ts_start=track.start_ts,
            region="singapore-live",
        )
    )
    session.add(
        TrackReview(
            review_id="queue-v1:0001",
            track_id=track.track_id,
            queue_order=1,
            stratum="pharos-alert",
            reviewer_id="primary",
            label="route_or_kinematic_anomaly",
            confidence="high",
            reviewed_at=NOW,
        )
    )
    session.commit()
    settings = _settings(tmp_path)

    first = run_pilot_evaluation(
        session, settings, now=NOW, write_outputs=False, verify_artifact=False
    )
    metrics = json.loads(json.dumps(first.metrics, sort_keys=True))
    second = run_pilot_evaluation(
        session, settings, now=NOW, write_outputs=False, verify_artifact=False
    )

    assert metrics == second.metrics
    assert second.metrics["status"] == "preliminary"
    assert second.metrics["external_official_recall"]["status"] == "not estimable"
    assert session.scalar(select(func.count()).select_from(EvaluationRun)) == 1
