from datetime import UTC, datetime, timedelta

import numpy as np
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from pharos.db.models import Position, Track
from pharos.ingest.persist import persist_scenario_or_positions
from pharos.ingest.synthetic import generate_scenario
from pharos.tracks.build import (
    build_tracks,
    kinematics,
    segment,
    track_features,
)
from pharos.tracks.incremental import rebuild_dirty_tracks


def _straight_track(mmsi: str, heading_deg: float, n: int = 12) -> list[Position]:
    """A clean straight-line track at a compass heading (for invariance tests)."""
    from pharos.ingest.synthetic import _step

    lat, lon = 1.2, 103.8
    ts = datetime(2023, 1, 1, tzinfo=UTC)
    pts = []
    for _ in range(n):
        pts.append(Position(mmsi=mmsi, ts=ts, lat=lat, lon=lon, sog=12.0))
        lat, lon = _step(lat, lon, heading_deg, 1.0)
        ts += timedelta(minutes=5)
    return pts


def test_segment_splits_on_gap() -> None:
    ts = datetime(2023, 1, 1, tzinfo=UTC)
    pts = [
        Position(mmsi="1", ts=ts, lat=1.0, lon=103.0),
        Position(mmsi="1", ts=ts + timedelta(minutes=5), lat=1.0, lon=103.0),
        Position(mmsi="1", ts=ts + timedelta(hours=4), lat=1.0, lon=103.0),  # 4h gap
    ]
    voyages = segment(pts, gap_minutes=60.0)
    assert len(voyages) == 2
    assert [len(v) for v in voyages] == [2, 1]


def test_features_length_and_heading_invariance() -> None:
    east = track_features(_straight_track("a", 90.0), seq_len=16)
    north = track_features(_straight_track("b", 0.0), seq_len=16)
    assert len(east) == 2 * 16 + 1
    # Two straight transits differing only in compass heading should map to near-identical
    # shape descriptors after the rotate-to-canonical normalization — the cross-region key.
    assert np.allclose(np.array(east[:-1]), np.array(north[:-1]), atol=0.05)


def test_anomaly_shape_differs_from_straight() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=1)
    straight = track_features(_straight_track("x", 90.0, n=40), seq_len=16)
    anom_pts = [
        p
        for p in sc.positions
        if p.mmsi == next(e.mmsi for e in sc.truth if e.event_type == "anomaly")
    ]
    anom = track_features(sorted(anom_pts, key=lambda p: p.ts), seq_len=16)
    # A zig-zag route is far from a straight transit in shape space.
    assert np.linalg.norm(np.array(anom[:-1]) - np.array(straight[:-1])) > 1.0


def test_kinematics_flags_impossible_jump() -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=1)
    spoof_mmsi = next(e.mmsi for e in sc.truth if e.event_type == "spoof")
    pts = sorted((p for p in sc.positions if p.mmsi == spoof_mmsi), key=lambda p: p.ts)
    kin = kinematics(pts)
    assert kin["max_implied_speed_kn"] > 60


def test_build_tracks_splits_gap_vessel(session: Session) -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=3)
    persist_scenario_or_positions(session, sc.vessels, sc.positions)
    session.commit()
    stats = build_tracks(session, region="singapore")
    session.commit()
    assert stats["tracks"] >= 1
    # The dark-ship vessel's 180-min silence splits it into 2 voyages.
    gap_mmsi = next(e.mmsi for e in sc.truth if e.event_type == "gap")
    gap_tracks = session.scalar(
        select(func.count()).select_from(Track).where(Track.mmsi == gap_mmsi)
    )
    assert gap_tracks == 2


def _track_snapshot(session: Session) -> list[tuple[object, ...]]:
    return [
        (
            track.track_id,
            track.mmsi,
            track.region,
            track.start_ts,
            track.end_ts,
            track.point_count,
            track.distance_km,
            track.features,
            track.sequence,
        )
        for track in session.scalars(select(Track).order_by(Track.track_id))
    ]


def test_incremental_tracks_match_full_recompute(session: Session) -> None:
    sc = generate_scenario("singapore", seed=4, n_normal=8)
    persist_scenario_or_positions(session, sc.vessels, sc.positions)
    session.commit()
    build_tracks(session, region="singapore")
    session.commit()
    expected = _track_snapshot(session)

    session.execute(delete(Track))
    session.commit()
    stats = rebuild_dirty_tracks(
        session,
        {vessel.mmsi for vessel in sc.vessels},
        region="singapore",
    )
    session.commit()

    assert stats["created"] == len(expected)
    assert _track_snapshot(session) == expected


def test_incremental_tracks_leave_clean_vessels_untouched(session: Session) -> None:
    sc = generate_scenario("singapore", seed=5, n_normal=4, with_events=False)
    persist_scenario_or_positions(session, sc.vessels, sc.positions)
    session.commit()
    build_tracks(session, region="singapore")
    session.commit()

    dirty_mmsi = sc.vessels[0].mmsi
    clean_before = [row for row in _track_snapshot(session) if row[1] != dirty_mmsi]
    last = session.scalar(
        select(Position).where(Position.mmsi == dirty_mmsi).order_by(Position.ts.desc()).limit(1)
    )
    assert last is not None
    appended = [
        Position(
            mmsi=dirty_mmsi,
            ts=last.ts + timedelta(minutes=5 * offset),
            lat=last.lat + offset * 0.001,
            lon=last.lon + offset * 0.001,
            sog=last.sog,
            source=last.source,
            region=last.region,
        )
        for offset in range(1, 7)
    ]
    from pharos.ingest.persist import persist_positions

    persist_positions(session, appended)
    session.commit()
    stats = rebuild_dirty_tracks(session, {dirty_mmsi}, region="singapore")
    session.commit()

    clean_after = [row for row in _track_snapshot(session) if row[1] != dirty_mmsi]
    assert stats["dirty_vessels"] == 1
    assert stats["updated"] >= 1
    assert clean_after == clean_before
