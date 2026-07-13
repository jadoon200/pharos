from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from pharos.db.models import Incident, Position, Track, Vessel, Zone


def test_vessel_position_track_incident_roundtrip(session: Session) -> None:
    ts = datetime(2023, 1, 1, 12, 0, tzinfo=UTC)
    session.add(Vessel(mmsi="565000001", name="TEST VESSEL", ship_type="tanker", flag="SG"))
    session.add(
        Position(mmsi="565000001", ts=ts, lat=1.2, lon=103.8, sog=0.2, cog=90.0, source="noaa")
    )
    session.add(
        Track(
            track_id="565000001:2023-01-01T12:00",
            mmsi="565000001",
            region="us-west",
            start_ts=ts,
            end_ts=ts,
            point_count=1,
            features=[0.1, 0.2, 0.3],  # JSON column round-trips on SQLite
        )
    )
    session.add(
        Incident(
            incident_id="inc-1",
            mmsi="565000001",
            detector="rendezvous",
            incident_type="ship-to-ship transfer",
            score=0.8,
            severity="high",
            reliability="C",
            ts_start=ts,
            lat=1.2,
            lon=103.8,
            counterpart_mmsi="565000002",
            techniques=["sts-transfer", "loitering"],
            evidence={"range_km": 0.2, "duration_min": 45},
        )
    )
    session.commit()

    v = session.get(Vessel, "565000001")
    assert v is not None and v.name == "TEST VESSEL"

    inc = session.scalars(select(Incident).where(Incident.detector == "rendezvous")).one()
    assert inc.evidence == {"range_km": 0.2, "duration_min": 45}
    assert inc.techniques == ["sts-transfer", "loitering"]
    assert inc.counterpart_mmsi == "565000002"

    trk = session.get(Track, "565000001:2023-01-01T12:00")
    assert trk is not None and trk.features == [0.1, 0.2, 0.3]


def test_zone_polygon_roundtrip(session: Session) -> None:
    session.add(
        Zone(
            zone_id="z1",
            name="Test Chokepoint",
            kind="chokepoint",
            polygon=[[1.1, 103.5], [1.1, 104.1], [1.3, 104.1]],
            sensitive=1,
        )
    )
    session.commit()
    z = session.get(Zone, "z1")
    assert z is not None and z.polygon[0] == [1.1, 103.5]
    assert z.sensitive == 1
