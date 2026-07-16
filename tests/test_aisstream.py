from pharos.ingest.aisstream import SUBSCRIPTION_MESSAGE_TYPES, parse_message

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
    assert position is not None
    assert position.lat == 1.25 and position.sog == 12.3
    assert position.source == "aisstream"
    assert position.region == "singapore-live"
    assert position.ts.year == 2024


def test_parse_standard_class_b_position_report() -> None:
    message = {
        **_MSG,
        "MessageType": "StandardClassBPositionReport",
        "Message": {
            "StandardClassBPositionReport": {
                "Sog": 7.5,
                "Cog": 181.0,
                "TrueHeading": 511,
            }
        },
    }

    parsed = parse_message(message)
    assert parsed is not None
    _, position = parsed
    assert position is not None
    assert position.sog == 7.5
    assert position.cog == 181.0
    assert position.heading is None  # AIS unavailable sentinel


def test_parse_static_message_enriches_identity_without_position() -> None:
    message = {
        "MessageType": "ShipStaticData",
        "MetaData": {"MMSI": 563123456, "ShipName": "META NAME"},
        "Message": {
            "ShipStaticData": {
                "Name": "STATIC NAME ",
                "CallSign": "9V1234 ",
                "Type": 80,
                "Dimension": {"A": 20, "B": 80, "C": 7, "D": 8},
            }
        },
    }

    parsed = parse_message(message)
    assert parsed is not None
    vessel, position = parsed
    assert position is None
    assert vessel.name == "STATIC NAME"
    assert vessel.call_sign == "9V1234"
    assert vessel.ship_type == "tanker"
    assert vessel.length == 100
    assert vessel.width == 15


def test_parse_message_ignores_other_types() -> None:
    assert parse_message({"MessageType": "AidToNavigationReport", "MetaData": {}}) is None


def test_parse_message_requires_position() -> None:
    assert parse_message({"MessageType": "PositionReport", "MetaData": {"MMSI": 1}}) is None


def test_subscription_requests_class_a_b_and_static() -> None:
    assert SUBSCRIPTION_MESSAGE_TYPES == (
        "PositionReport",
        "StandardClassBPositionReport",
        "ShipStaticData",
    )
