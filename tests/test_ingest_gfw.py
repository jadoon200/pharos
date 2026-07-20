import json

import httpx
import respx

from pharos.config import get_settings
from pharos.ingest.gfw import fetch_events, parse_events

_PAYLOAD = {
    "entries": [
        {
            "id": "encounter-1",
            "type": "encounter",
            "start": "2023-01-01T00:00:00Z",
            "end": "2023-01-01T01:00:00Z",
            "position": {"lat": 1.17, "lon": 103.82},
            "vessel": {"ssvid": "563000010"},
        },
        {
            "id": "loiter-1",
            "type": "loitering",
            "start": "2023-01-01T02:00:00Z",
            "position": {"lat": 1.2, "lon": 103.8},
            "vessel": {"mmsi": "563000011"},
        },
        {
            "id": "port-1",
            "type": "port_visit",
            "start": "2023-01-01T03:00:00Z",
            "vessel": {"ssvid": "1"},
        },
    ]
}


def test_parse_events_maps_and_filters() -> None:
    events = parse_events(_PAYLOAD)
    assert [e.event_type for e in events] == ["rendezvous", "loiter", "port-visit"]
    assert events[0].event_id == "encounter-1"
    assert events[0].mmsi == "563000010"
    assert events[0].start is not None and events[0].lat == 1.17


def test_fetch_events_no_token_returns_empty() -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.gfw_token = ""  # explicit
    assert fetch_events("rendezvous", "2023-01-01", "2023-01-02") == []


@respx.mock
def test_fetch_events_mocked() -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.gfw_token = "test-token"
    route = respx.post(f"{settings.gfw_api_url}/events").mock(
        return_value=httpx.Response(200, json=_PAYLOAD)
    )
    events = fetch_events("rendezvous", "2023-01-01", "2023-01-02", bbox=(1.1, 103.5, 1.3, 104.1))
    assert route.called
    assert len(events) == 3
    request = route.calls[0].request
    assert request.url.params["offset"] == "0"
    assert request.url.params["limit"] == "1000"
    assert request.url.params["start-date"] == "2023-01-01"
    assert request.url.params["end-date"] == "2023-01-02"
    assert request.url.params["sort"] == "-start"
    body = json.loads(request.content)
    assert body["datasets"] == ["public-global-encounters-events:latest"]
    get_settings.cache_clear()


@respx.mock
def test_fetch_events_paginates() -> None:
    get_settings.cache_clear()
    settings = get_settings()
    settings.gfw_token = "test-token"
    first = {**_PAYLOAD, "nextOffset": 2, "total": 3}
    second = {"entries": [_PAYLOAD["entries"][0]], "nextOffset": 3, "total": 3}
    route = respx.post(f"{settings.gfw_api_url}/events").mock(
        side_effect=[httpx.Response(200, json=first), httpx.Response(200, json=second)]
    )
    events = fetch_events("gap", "2023-01-01", "2023-01-02")
    assert len(events) == 4
    assert route.call_count == 2
    assert route.calls[1].request.url.params["offset"] == "2"
    body = json.loads(route.calls[1].request.content)
    assert body["datasets"] == ["public-global-gaps-events:latest"]
    get_settings.cache_clear()
