from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pharos.db.models import Position, Vessel, Zone
from pharos.ingest.persist import ensure_vessels, persist_scenario_or_positions
from pharos.ingest.reference import seed_zones
from pharos.ingest.synthetic import generate_scenario


def test_persist_is_idempotent(session: Session) -> None:
    sc = generate_scenario("singapore", seed=0, n_normal=3)
    first = persist_scenario_or_positions(session, sc.vessels, sc.positions)
    session.commit()
    assert first["new"] == len(sc.positions)
    assert first["vessels"] == len(sc.vessels)

    # Re-persist the same scenario → everything is skipped, no duplicate track points.
    second = persist_scenario_or_positions(session, sc.vessels, sc.positions)
    session.commit()
    assert second["new"] == 0
    assert second["skipped"] == len(sc.positions)
    total = session.scalar(select(func.count()).select_from(Position))
    assert total == len(sc.positions)


def test_seed_zones_idempotent(session: Session) -> None:
    n1 = seed_zones(session)
    session.commit()
    n2 = seed_zones(session)
    session.commit()
    assert n1 == n2
    stored = session.scalar(select(func.count()).select_from(Zone))
    assert stored == n1
    z = session.get(Zone, "singapore-strait")
    assert z is not None and z.sensitive == 1 and len(z.polygon) >= 3


def test_vessel_upsert_handles_sqlite_timezone_roundtrip(session: Session) -> None:
    first = datetime(2026, 7, 16, 1, 0, tzinfo=UTC)
    session.add(Vessel(mmsi="563123456", first_seen=first, last_seen=first))
    session.commit()
    session.expire_all()  # SQLite reloads DateTime(timezone=True) as a naive value.

    later = first + timedelta(minutes=1)
    ensure_vessels(
        session,
        [Vessel(mmsi="563123456", first_seen=first, last_seen=later)],
    )
    session.commit()

    stored = session.get(Vessel, "563123456")
    assert stored is not None and stored.last_seen is not None
    assert stored.last_seen.replace(tzinfo=UTC) == later
