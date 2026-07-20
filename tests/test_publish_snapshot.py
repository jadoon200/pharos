from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from pharos.config import Settings
from pharos.db.models import CollectorRun, Incident, Position, Track, Vessel
from pharos.geo import simplify_polyline
from pharos.publish.snapshot import build_snapshots, sanitize

NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def test_simplify_polyline_retains_shape_and_endpoints() -> None:
    lat, lon = simplify_polyline(
        [1.0, 1.0, 1.01, 1.0, 1.0], [103, 103.01, 103.02, 103.03, 103.04], 0.2
    )
    assert (lat[0], lon[0]) == (1.0, 103.0)
    assert (lat[-1], lon[-1]) == (1.0, 103.04)
    assert 1.01 in lat
    straight_lat, straight_lon = simplify_polyline([1, 1, 1], [103, 103.01, 103.02], 0.01)
    assert straight_lat == [1.0, 1.0]
    assert straight_lon == [103.0, 103.02]


def test_sanitizer_fails_closed_and_rounds_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        _env_file=None,
        aisstream_key="actual-ais-secret",
        gfw_token="actual-gfw-secret",
    )
    monkeypatch.setattr("pharos.publish.snapshot.get_settings", lambda: settings)
    safe = sanitize({"lat": 1.123456, "geometry": {"coordinates": [[103.123456, 1.234567]]}})
    assert safe == {"lat": 1.1235, "geometry": {"coordinates": [[103.1235, 1.2346]]}}

    adversarial = [
        {"api_key": "value"},
        {"note": "Bearer abcdefghijklmnop"},
        {"note": "sk-abcdefghijklmno"},
        {"note": "/Users/example/project/.env"},
        {"MessageType": "PositionReport"},
        {"MetaData": {"MMSI": 1}},
        {"note": "Traceback (most recent call last)"},
        {"note": "SELECT * FROM sqlite_master"},
        {"note": "sqlite:///secret.db"},
        {"note": "actual-ais-secret"},
        {"raw": {"envelope": True}},
    ]
    for payload in adversarial:
        with pytest.raises(ValueError):
            sanitize(payload)


def test_snapshot_generator_is_bounded_delayed_and_secret_free(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite:///:memory:",
        aisstream_key="snapshot-ais-secret",
        gfw_token="snapshot-gfw-secret",
        anomaly_model_dir=tmp_path / "missing-models",
        collector_region="singapore-live",
        pilot_start_at=NOW - timedelta(days=4),
        snapshot_track_limit=200,
        snapshot_points_per_track=150,
    )
    monkeypatch.setattr("pharos.publish.snapshot.get_settings", lambda: settings)
    vessel = Vessel(mmsi="563000001", name="PRIVATE NAME", ship_type="cargo")
    session.add(vessel)
    run = CollectorRun(
        started_at=NOW - timedelta(hours=6),
        last_message_at=NOW - timedelta(minutes=2),
        report_count=20,
        vessel_count=1,
        status="running",
    )
    session.add(run)
    track = Track(
        track_id="563000001:private-track",
        mmsi=vessel.mmsi,
        region="singapore-live",
        start_ts=NOW - timedelta(hours=2),
        end_ts=NOW - timedelta(minutes=20),
        point_count=4,
        distance_km=5,
        start_lat=1.2,
        start_lon=103.8,
        end_lat=1.23,
        end_lon=103.83,
        sequence=[[1, 0, 1, 0]],
    )
    session.add(track)
    for index in range(4):
        session.add(
            Position(
                mmsi=vessel.mmsi,
                ts=track.start_ts + timedelta(minutes=index * 20),
                lat=1.2 + index * 0.01,
                lon=103.8 + index * 0.01,
                source="aisstream",
                region="singapore-live",
                raw={"MessageType": "must never publish"},
            )
        )
    session.add(
        Incident(
            incident_id="private-incident",
            mmsi=vessel.mmsi,
            track_id=track.track_id,
            detector="loiter",
            incident_type="loiter candidate",
            score=0.8,
            severity="moderate",
            reliability="C",
            ts_start=track.start_ts,
            lat=1.234567,
            lon=103.876543,
            region="singapore-live",
            evidence={"duration_minutes": 80, "private": "do not publish"},
        )
    )
    session.commit()
    out = tmp_path / "snapshots"

    sizes = build_snapshots(session, settings, out, now=NOW)

    assert set(sizes) == {
        "status.json",
        "stats.json",
        "tracks.json",
        "incidents.json",
        "evaluations.json",
        "model.json",
    }
    tracks = json.loads((out / "tracks.json").read_text())
    assert len(tracks["features"]) == 1
    assert (
        tracks["features"][0]["properties"]["end_ts"] <= (NOW - timedelta(minutes=15)).isoformat()
    )
    incident = json.loads((out / "incidents.json").read_text())["incidents"][0]
    assert incident["lat"] == 1.2346 and incident["lon"] == 103.8765
    assert incident["evidence_summary"] == {"duration_minutes": 80}
    published = b"".join(path.read_bytes() for path in out.glob("*.json"))
    for forbidden in (
        b"snapshot-ais-secret",
        b"snapshot-gfw-secret",
        b"PRIVATE NAME",
        b"563000001",
        b"MessageType",
        b"private-track",
        b"private-incident",
    ):
        assert forbidden not in published
