"""Global Fishing Watch events client — free silver labels for the eval cross-check.

GFW derives maritime events from AIS — **encounters** (≈ ship-to-ship rendezvous), **loitering**,
**gap** (AIS-off) events, and port visits — and exposes them through a free API (registration
token). PHAROS uses these as an independent, real-world cross-check on its own detectors in the
eval (`docs/EVAL.md`): the GFW *gap* events in particular model reception, so they help separate a
genuine dark-ship from a coverage artifact.

Opt-in and non-fatal: with no `PHAROS_GFW_TOKEN` the client returns [], so the offline path (NOAA
+ synthetic labels) never depends on it. https://globalfishingwatch.org/our-apis/
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from pharos.config import get_settings
from pharos.logging import get_logger

log = get_logger(__name__)

# GFW event type → PHAROS detector name (the cross-check mapping).
_TYPE_MAP = {
    "encounter": "rendezvous",
    "loitering": "loiter",
    "gap": "gap",
}


@dataclass(frozen=True)
class GfwEvent:
    event_type: str  # PHAROS detector name (rendezvous|loiter|gap)
    mmsi: str | None
    start: datetime | None
    end: datetime | None
    lat: float | None
    lon: float | None
    raw: dict[str, Any]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_events(payload: dict[str, Any]) -> list[GfwEvent]:
    """Normalize a GFW /events response into GfwEvents (defensive to schema variation)."""
    out: list[GfwEvent] = []
    for entry in payload.get("entries", payload.get("data", [])):
        raw_type = str(entry.get("type", "")).lower()
        mapped = _TYPE_MAP.get(raw_type)
        if mapped is None:
            continue
        pos = entry.get("position") or {}
        vessel = entry.get("vessel") or {}
        out.append(
            GfwEvent(
                event_type=mapped,
                mmsi=str(vessel.get("ssvid") or vessel.get("mmsi") or "") or None,
                start=_parse_ts(entry.get("start")),
                end=_parse_ts(entry.get("end")),
                lat=pos.get("lat"),
                lon=pos.get("lon"),
                raw=entry,
            )
        )
    return out


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10), reraise=True)
def fetch_events(
    event_type: str,
    start_date: str,
    end_date: str,
    bbox: tuple[float, float, float, float] | None = None,
) -> list[GfwEvent]:
    """Fetch GFW events of `event_type` (rendezvous|loiter|gap) in a date range / bbox.

    Returns [] (never raises for the missing-token case) so the eval degrades gracefully.
    """
    settings = get_settings()
    if not settings.gfw_token:
        log.info("gfw_skip", reason="no token")
        return []
    gfw_type = {v: k for k, v in _TYPE_MAP.items()}.get(event_type, event_type)
    body: dict[str, Any] = {
        "datasets": [f"public-global-{gfw_type}-events:latest"],
        "startDate": start_date,
        "endDate": end_date,
    }
    if bbox is not None:
        min_lat, min_lon, max_lat, max_lon = bbox
        body["geometry"] = {
            "type": "Polygon",
            "coordinates": [
                [
                    [min_lon, min_lat],
                    [max_lon, min_lat],
                    [max_lon, max_lat],
                    [min_lon, max_lat],
                    [min_lon, min_lat],
                ]
            ],
        }
    headers = {"Authorization": f"Bearer {settings.gfw_token}"}
    with httpx.Client(timeout=settings.http_timeout_seconds) as client:
        resp = client.post(f"{settings.gfw_api_url}/events", json=body, headers=headers)
        resp.raise_for_status()
        events = parse_events(resp.json())
    log.info("gfw_fetched", event_type=event_type, count=len(events))
    return events
