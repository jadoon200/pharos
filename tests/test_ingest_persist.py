from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pharos.db.models import Position, Zone
from pharos.ingest.persist import persist_scenario_or_positions
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
