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
from datetime import UTC, date, datetime
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
    "port_visit": "port-visit",
    "port-visit": "port-visit",
    "portvisit": "port-visit",
}

# PHAROS detector name -> current GFW v3 dataset alias. Keep these explicit: the API's encounter
# and gap dataset names are plural even though response event types are singular.
_DATASET_MAP = {
    "rendezvous": "public-global-encounters-events:latest",
    "loiter": "public-global-loitering-events:latest",
    "gap": "public-global-gaps-events:latest",
    "port-visit": "public-global-port-visits-events:latest",
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
    event_id: str | None = None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_in_window(event: GfwEvent, start_date: str, end_date: str) -> bool:
    if event.start is None:
        return False
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    event_start = event.start.astimezone(UTC).date()
    return start <= event_start < end


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
                event_id=str(entry.get("id") or "") or None,
            )
        )
    return out


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=10), reraise=True)
def _post_page(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, str | int],
    body: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    """Retry one page so a late-page timeout does not restart an entire large query."""
    response = client.post(url, params=params, json=body, headers=headers)
    response.raise_for_status()
    payload: dict[str, Any] = response.json()
    return payload


def fetch_events(
    event_type: str,
    start_date: str,
    end_date: str,
    bbox: tuple[float, float, float, float] | None = None,
) -> list[GfwEvent]:
    """Fetch GFW events of `event_type` in a date range / bbox.

    Returns [] for the missing-token case. Callers at an optional integration boundary should
    catch network/API errors so the offline evaluation remains usable.
    """
    settings = get_settings()
    if not settings.gfw_token:
        log.info("gfw_skip", reason="no token")
        return []
    dataset = _DATASET_MAP.get(event_type)
    if dataset is None:
        raise ValueError(f"unsupported GFW event type: {event_type}")
    body: dict[str, Any] = {
        "datasets": [dataset],
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
    events: list[GfwEvent] = []
    offset = 0
    limit = 1_000
    timeout = httpx.Timeout(
        settings.http_timeout_seconds,
        read=max(90.0, settings.http_timeout_seconds),
    )
    with httpx.Client(timeout=timeout) as client:
        while True:
            payload = _post_page(
                client,
                f"{settings.gfw_api_url}/events",
                params={
                    "offset": offset,
                    "limit": limit,
                    "start-date": start_date,
                    "end-date": end_date,
                    "sort": "-start",
                },
                body=body,
                headers=headers,
            )
            page = [
                event
                for event in parse_events(payload)
                if _event_in_window(event, start_date, end_date)
            ]
            events.extend(page)

            next_offset = payload.get("nextOffset")
            total = payload.get("total")
            if (
                not page
                or not isinstance(next_offset, int)
                or next_offset <= offset
                or (isinstance(total, int) and next_offset >= total)
            ):
                break
            offset = next_offset
    log.info("gfw_fetched", event_type=event_type, count=len(events))
    return events
