from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pharos.db.models import Position, Vessel
from pharos.ingest.noaa import load_csv
from pharos.ingest.persist import persist_scenario_or_positions

_CSV = (
    "MMSI,BaseDateTime,LAT,LON,SOG,COG,Heading,VesselName,IMO,CallSign,"
    "VesselType,Status,Length,Width,Draft,Cargo,TransceiverClass\n"
    "366000001,2023-01-01T00:00:03,33.70,-118.20,12.1,90,91,CARGO,IMO1,WDA1,70,0,200,30,10,,A\n"
    "366000001,2023-01-01T00:05:03,33.71,-118.19,12.0,90,91,CARGO,IMO1,WDA1,70,0,200,30,10,,A\n"
)


def test_load_csv_then_persist(tmp_path: Path, session: Session) -> None:
    csv_path = tmp_path / "slice.csv"
    csv_path.write_text(_CSV)
    vessels, positions = load_csv(csv_path, region="us-west")
    assert len(vessels) == 1 and len(positions) == 2

    stats = persist_scenario_or_positions(session, vessels, positions)
    session.commit()
    assert stats["new"] == 2 and stats["vessels"] == 1
    assert session.scalar(select(func.count()).select_from(Vessel)) == 1
    assert session.scalar(select(func.count()).select_from(Position)) == 2
