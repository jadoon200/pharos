"""AISStream.io live AIS lane — the global demo stream (includes Malacca / Singapore).

AISStream.io is a free WebSocket AIS feed (free API key). This module captures a bounded window
of live position reports into the corpus, so the demo can show a live maritime picture over the
Singapore Strait rather than only the offline NOAA slice. Opt-in: needs the `live` extra
(`websockets`) + `PHAROS_AISSTREAM_KEY`; the whole product runs without it on NOAA bulk.

The message parser (`parse_message`) is pure and unit-tested; the connect loop is a thin async
wrapper around it, so the testable logic doesn't require a live socket.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from pharos.config import get_settings
from pharos.db.base import session_scope
from pharos.db.models import Position, Vessel
from pharos.ingest.noaa import flag_for_mmsi, ship_type_label
from pharos.ingest.persist import ensure_vessels, persist_positions
from pharos.logging import configure_logging, get_logger

log = get_logger(__name__)


POSITION_MESSAGE_TYPES = ("PositionReport", "StandardClassBPositionReport")
SUBSCRIPTION_MESSAGE_TYPES = (*POSITION_MESSAGE_TYPES, "ShipStaticData")
ParsedMessage = tuple[Vessel, Position | None]


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _identity_vessel(mmsi: str, meta: dict[str, Any], report: dict[str, Any]) -> Vessel:
    dimensions = report.get("Dimension") or {}
    length_parts = (_number(dimensions.get("A")), _number(dimensions.get("B")))
    width_parts = (_number(dimensions.get("C")), _number(dimensions.get("D")))
    length = sum(part for part in length_parts if part is not None) or None
    width = sum(part for part in width_parts if part is not None) or None
    return Vessel(
        mmsi=mmsi,
        name=_text(report.get("Name")) or _text(meta.get("ShipName")),
        call_sign=_text(report.get("CallSign")),
        ship_type=ship_type_label(_number(report.get("Type"))),
        flag=flag_for_mmsi(mmsi),
        length=length,
        width=width,
    )


def parse_message(msg: dict[str, Any]) -> ParsedMessage | None:
    """Turn one supported AISStream envelope into vessel identity and optional position.

    AISStream envelopes look like:
        {"MessageType": "PositionReport",
         "MetaData": {"MMSI": 563123456, "ShipName": "X", "latitude":.., "longitude":..,
                      "time_utc": "..."},
         "Message": {"PositionReport": {"Sog": 12.3, "Cog": 90, "TrueHeading": 91}}}
    Class A and standard Class B reports produce a position. `ShipStaticData` produces only
    advisory vessel identity enrichment; malformed static data never blocks position handling.
    A position report with missing/invalid coordinates still salvages the vessel identity —
    only the (unusable) position is dropped.
    """
    message_type = msg.get("MessageType")
    if message_type not in SUBSCRIPTION_MESSAGE_TYPES:
        return None
    meta = msg.get("MetaData") or {}
    mmsi = str(meta.get("MMSI") or "").strip()
    if not mmsi:
        return None
    report = (msg.get("Message") or {}).get(message_type) or {}
    vessel = _identity_vessel(mmsi, meta, report)
    if message_type == "ShipStaticData":
        return vessel, None

    lat = meta.get("latitude")
    lon = meta.get("longitude")
    latitude = _number(lat)
    longitude = _number(lon)
    if (
        latitude is None
        or longitude is None
        or not -90.0 <= latitude <= 90.0
        or not -180.0 <= longitude <= 180.0
    ):
        return vessel, None
    ts = _parse_ts(meta.get("time_utc")) or datetime.now(UTC)
    vessel.first_seen = ts
    vessel.last_seen = ts
    sog = _number(report.get("Sog"))
    cog = _number(report.get("Cog"))
    heading = _number(report.get("TrueHeading"))
    position = Position(
        mmsi=mmsi,
        ts=ts,
        lat=latitude,
        lon=longitude,
        sog=sog if sog is None or sog < 102.3 else None,
        cog=cog if cog is None or cog < 360.0 else None,
        heading=heading if heading is None or heading < 360.0 else None,
        nav_status=_text(report.get("NavigationalStatus")),
        source="aisstream",
        region="singapore-live",
    )
    return vessel, position


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    # AISStream time_utc: "2024-01-01 12:00:00.000000000 +0000 UTC" — take the ISO-ish head.
    head = value.split(".")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(head, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


async def capture(seconds: float | None = None) -> int:
    """Connect, capture position reports for a bounded window, persist them. Returns the count.

    Requires the `live` extra (websockets) and PHAROS_AISSTREAM_KEY. Best-effort: any socket
    error is logged and capture ends with whatever was collected.
    """
    settings = get_settings()
    if not settings.aisstream_key:
        log.warning("aisstream_skip", reason="no PHAROS_AISSTREAM_KEY set")
        return 0
    try:
        import websockets  # optional dep (the `live` extra)
    except ImportError:
        log.warning("aisstream_skip", reason="websockets not installed (pip install .[live])")
        return 0

    window = seconds if seconds is not None else settings.aisstream_seconds
    subscribe = {
        "APIKey": settings.aisstream_key,
        "BoundingBoxes": settings.aisstream_bbox,
        "FilterMessageTypes": list(SUBSCRIPTION_MESSAGE_TYPES),
    }
    vessels: dict[str, Vessel] = {}
    positions: list[Position] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + window
    async with websockets.connect(settings.aisstream_url) as ws:
        await ws.send(json.dumps(subscribe))
        while loop.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - loop.time()))
            except TimeoutError:
                break
            parsed = parse_message(json.loads(raw))
            if parsed is None:
                continue
            vessel, position = parsed
            vessels.setdefault(vessel.mmsi, vessel)
            if position is not None:
                positions.append(position)

    with session_scope() as session:
        ensure_vessels(session, list(vessels.values()))
        stats = persist_positions(session, positions)
    log.info("aisstream_capture_complete", **stats)
    return stats["new"]


def main() -> None:
    configure_logging()
    asyncio.run(capture())


if __name__ == "__main__":
    main()
