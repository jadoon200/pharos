import httpx
import respx

from pharos.config import get_settings
from pharos.ingest.gfw import fetch_events, parse_events

_PAYLOAD = {
    "entries": [
        {
            "type": "encounter",
            "start": "2023-01-01T00:00:00Z",
            "end": "2023-01-01T01:00:00Z",
            "position": {"lat": 1.17, "lon": 103.82},
            "vessel": {"ssvid": "563000010"},
        },
        {
            "type": "loitering",
            "start": "2023-01-01T02:00:00Z",
            "position": {"lat": 1.2, "lon": 103.8},
            "vessel": {"mmsi": "563000011"},
        },
        {"type": "port_visit", "vessel": {"ssvid": "1"}},  # unmapped → dropped
    ]
}


def test_parse_events_maps_and_filters() -> None:
    events = parse_events(_PAYLOAD)
    assert [e.event_type for e in events] == ["rendezvous", "loiter"]
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
    assert len(events) == 2
    get_settings.cache_clear()
