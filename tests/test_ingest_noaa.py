import csv
import io
from datetime import UTC

from pharos.ingest.noaa import flag_for_mmsi, parse_rows, ship_type_label

# Trimmed to the columns the loader reads (through Width) — DictReader stays consistent.
_HEADER = (
    "MMSI,BaseDateTime,LAT,LON,SOG,COG,Heading,"
    "VesselName,IMO,CallSign,VesselType,Status,Length,Width"
)
_ROWS = "\n".join(
    [
        _HEADER,
        "366000001,2023-01-01T00:00:03,33.70,-118.20,12.1,90,91,CARGO,IMO1,WDA1,70,0,200,30",
        "366000001,2023-01-01T00:05:03,33.71,-118.19,12.0,90,91,CARGO,IMO1,WDA1,70,0,200,30",
        # blank lat/lon → skipped
        "366000002,2023-01-01T00:00:03,,,0.0,,,,,,,,,",
        "563000009,2023-01-01T00:10:00,1.25,103.80,0.2,,,TANKER SG,,,80,1,250,40",
    ]
)


def test_parse_rows_skips_bad_and_builds_vessels() -> None:
    reader = csv.DictReader(io.StringIO(_ROWS))
    vessels, positions = parse_rows(reader, region="us-west")
    assert set(vessels) == {"366000001", "563000009"}  # bad row dropped
    assert len(positions) == 3
    v = vessels["366000001"]
    assert v.ship_type == "cargo"
    assert v.flag == "US"
    assert v.first_seen is not None and v.last_seen is not None
    assert positions[0].ts.hour == 0
    assert positions[0].ts.tzinfo is UTC
    assert all(p.region == "us-west" and p.source == "noaa" for p in positions)


def test_ship_type_label() -> None:
    assert ship_type_label(70) == "cargo"
    assert ship_type_label(80) == "tanker"
    assert ship_type_label(30) == "fishing"
    assert ship_type_label(None) is None


def test_flag_for_mmsi() -> None:
    assert flag_for_mmsi("563000009") == "SG"
    assert flag_for_mmsi("366000001") == "US"
    assert flag_for_mmsi("999000000") is None
    assert flag_for_mmsi("xx") is None
