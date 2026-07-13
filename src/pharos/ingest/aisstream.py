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
from pharos.ingest.noaa import flag_for_mmsi
from pharos.ingest.persist import persist_positions
from pharos.logging import configure_logging, get_logger

log = get_logger(__name__)


def parse_message(msg: dict[str, Any]) -> tuple[Vessel, Position] | None:
    """Turn one AISStream `PositionReport` message into (Vessel, Position), or None.

    AISStream envelopes look like:
        {"MessageType": "PositionReport",
         "MetaData": {"MMSI": 563123456, "ShipName": "X", "latitude":.., "longitude":..,
                      "time_utc": "..."},
         "Message": {"PositionReport": {"Sog": 12.3, "Cog": 90, "TrueHeading": 91}}}
    """
    if msg.get("MessageType") != "PositionReport":
        return None
    meta = msg.get("MetaData") or {}
    mmsi = str(meta.get("MMSI") or "").strip()
    lat = meta.get("latitude")
    lon = meta.get("longitude")
    if not mmsi or lat is None or lon is None:
        return None
    report = (msg.get("Message") or {}).get("PositionReport") or {}
    ts = _parse_ts(meta.get("time_utc")) or datetime.now(UTC)
    vessel = Vessel(mmsi=mmsi, name=(meta.get("ShipName") or None), flag=flag_for_mmsi(mmsi))
    position = Position(
        mmsi=mmsi,
        ts=ts,
        lat=float(lat),
        lon=float(lon),
        sog=report.get("Sog"),
        cog=report.get("Cog"),
        heading=report.get("TrueHeading"),
        source="aisstream",
        region="live",
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
        "FilterMessageTypes": ["PositionReport"],
    }
    vessels: dict[str, Vessel] = {}
    positions: list[Position] = []
    deadline = asyncio.get_event_loop().time() + window
    async with websockets.connect(settings.aisstream_url) as ws:
        await ws.send(json.dumps(subscribe))
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=max(1.0, deadline - asyncio.get_event_loop().time())
                )
            except TimeoutError:
                break
            parsed = parse_message(json.loads(raw))
            if parsed is None:
                continue
            vessel, position = parsed
            vessels.setdefault(vessel.mmsi, vessel)
            positions.append(position)

    with session_scope() as session:
        from pharos.ingest.persist import ensure_vessels

        ensure_vessels(session, list(vessels.values()))
        stats = persist_positions(session, positions)
    log.info("aisstream_capture_complete", **stats)
    return stats["new"]


def main() -> None:
    configure_logging()
    asyncio.run(capture())


if __name__ == "__main__":
    main()
