from pharos.ingest.aisstream import parse_message

_MSG = {
    "MessageType": "PositionReport",
    "MetaData": {
        "MMSI": 563123456,
        "ShipName": "LIVE VESSEL",
        "latitude": 1.25,
        "longitude": 103.81,
        "time_utc": "2024-01-01 12:00:00.000000000 +0000 UTC",
    },
    "Message": {"PositionReport": {"Sog": 12.3, "Cog": 90.0, "TrueHeading": 91}},
}


def test_parse_message_position_report() -> None:
    parsed = parse_message(_MSG)
    assert parsed is not None
    vessel, position = parsed
    assert vessel.mmsi == "563123456"
    assert vessel.flag == "SG"
    assert position.lat == 1.25 and position.sog == 12.3
    assert position.source == "aisstream"
    assert position.ts.year == 2024


def test_parse_message_ignores_other_types() -> None:
    assert parse_message({"MessageType": "ShipStaticData", "MetaData": {}}) is None


def test_parse_message_requires_position() -> None:
    assert parse_message({"MessageType": "PositionReport", "MetaData": {"MMSI": 1}}) is None
