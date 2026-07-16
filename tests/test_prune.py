from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pharos.collector.retention import prune_positions
from pharos.config import Settings
from pharos.db.base import Base, make_engine
from pharos.db.models import Position, Vessel


def test_prune_deletes_only_expired_live_positions(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'prune.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(Vessel(mmsi="563123456"))
        session.add_all(
            [
                Position(
                    mmsi="563123456",
                    ts=now - timedelta(days=22),
                    lat=1.2,
                    lon=103.8,
                    source="aisstream",
                ),
                Position(
                    mmsi="563123456",
                    ts=now - timedelta(days=22),
                    lat=1.2,
                    lon=103.8,
                    source="noaa",
                ),
                Position(
                    mmsi="563123456",
                    ts=now - timedelta(days=1),
                    lat=1.2,
                    lon=103.8,
                    source="aisstream",
                ),
            ]
        )
        session.commit()
    settings = Settings(_env_file=None, database_url=url, retention_positions_days=21)

    stats = prune_positions(engine, settings)

    with Session(engine) as session:
        remaining = session.scalar(select(func.count()).select_from(Position))
    assert stats["deleted"] == 1
    assert remaining == 2
    engine.dispose()
