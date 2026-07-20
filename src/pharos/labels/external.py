"""Import attributed GFW events and manual-structured ReCAAP/TSIB incident labels."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from pharos.config import Settings, get_settings
from pharos.db.base import init_sqlite_schema, session_scope
from pharos.db.models import ExternalEvent
from pharos.ingest.gfw import fetch_events
from pharos.logging import configure_logging, get_logger
from pharos.timeutil import utc_aware

log = get_logger(__name__)

GFW_ATTRIBUTION = "Global Fishing Watch data — https://globalfishingwatch.org"
RECAAP_ATTRIBUTION = "Source: ReCAAP Information Sharing Centre."
TSIB_ATTRIBUTION = "Source: Transport Safety Investigation Bureau, Singapore."

_GFW_SOURCE = {
    "rendezvous": "gfw-encounter",
    "loiter": "gfw-loiter",
    "gap": "gfw-gap",
    "port-visit": "gfw-port-visit",
}
_NORMALIZED_GFW_TYPE = {
    "rendezvous": "rendezvous",
    "loiter": "loiter",
    "gap": "gap",
    "port-visit": "port-visit",
}


class ManualEvent(BaseModel):
    """Strict minimal incident transcription; prose is deliberately bounded."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["recaap", "tsib"]
    source_ref: str = Field(min_length=1, max_length=255)
    source_url: AnyHttpUrl
    event_type: Literal["incident-robbery", "incident-collision", "incident-other"]
    occurred_start: datetime
    occurred_end: datetime | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    mmsi: str | None = Field(default=None, max_length=16)
    imo: str | None = Field(default=None, max_length=16)
    vessel_name: str | None = Field(default=None, max_length=255)
    source_confidence: Literal["official"]
    retrieved: date | datetime
    notes: str = Field(max_length=300)

    @field_validator("occurred_start", "occurred_end")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("event timestamps must include a UTC offset")
        return value


def _retrieved_at(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime(value.year, value.month, value.day, tzinfo=UTC)


def import_gfw_events(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Fetch the exact pilot window/bbox and idempotently persist GFW silver labels."""
    now = now or datetime.now(UTC)
    start = settings.pilot_start_at or datetime(2026, 7, 16, 2, 47, 51, tzinfo=UTC)
    box = settings.aisstream_bbox[0]
    bbox = (box[0][0], box[0][1], box[1][0], box[1][1])
    counts: dict[str, int] = {}
    for event_type, source in _GFW_SOURCE.items():
        events = fetch_events(
            event_type,
            start.date().isoformat(),
            (now.date() + timedelta(days=1)).isoformat(),
            bbox,
        )
        stored = 0
        skipped = 0
        for event in events:
            if event.event_id is None or event.start is None:
                skipped += 1
                continue
            inside_window = utc_aware(start) <= utc_aware(event.start) <= utc_aware(now)
            inside_bbox = (
                event.lat is not None
                and event.lon is not None
                and bbox[0] <= event.lat <= bbox[2]
                and bbox[1] <= event.lon <= bbox[3]
            )
            if not inside_window or not inside_bbox:
                skipped += 1
                continue
            raw = {
                key: value
                for key, value in event.raw.items()
                if key in {"id", "type", "start", "end", "position", "vessel", "port"}
            }
            session.merge(
                ExternalEvent(
                    event_id=f"{source}:{event.event_id}",
                    source=source,
                    source_ref=event.event_id,
                    source_url="https://globalfishingwatch.org/our-apis/",
                    event_type=_NORMALIZED_GFW_TYPE[event_type],
                    mmsi=event.mmsi,
                    ts_start=event.start,
                    ts_end=event.end,
                    lat=event.lat,
                    lon=event.lon,
                    source_confidence="silver",
                    retrieved_at=now,
                    attribution=GFW_ATTRIBUTION,
                    raw=raw,
                )
            )
            stored += 1
        session.commit()
        counts[source] = stored
        log.info("gfw_labels_imported", source=source, count=stored, filtered=skipped)
    return counts


def _yaml_files(directory: Path) -> list[Path]:
    if directory.name in {"recaap", "tsib"}:
        return sorted(directory.glob("*.yaml"))
    return sorted([*directory.glob("recaap/*.yaml"), *directory.glob("tsib/*.yaml")])


def import_yaml_events(session: Session, directory: Path) -> dict[str, int]:
    """Validate and upsert all manual incident files; any malformed file fails the import."""
    counts = {"recaap": 0, "tsib": 0}
    for path in _yaml_files(directory):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        event = ManualEvent.model_validate(payload)
        if path.parent.name != event.source:
            raise ValueError(f"{path}: source must match parent directory")
        attribution = RECAAP_ATTRIBUTION if event.source == "recaap" else TSIB_ATTRIBUTION
        session.merge(
            ExternalEvent(
                event_id=f"{event.source}:{event.source_ref}",
                source=event.source,
                source_ref=event.source_ref,
                source_url=str(event.source_url),
                event_type=event.event_type,
                mmsi=event.mmsi,
                imo=event.imo,
                vessel_name=event.vessel_name,
                ts_start=event.occurred_start,
                ts_end=event.occurred_end,
                lat=event.lat,
                lon=event.lon,
                source_confidence=event.source_confidence,
                retrieved_at=_retrieved_at(event.retrieved),
                attribution=attribution,
                raw={"notes": event.notes},
            )
        )
        counts[event.source] += 1
    session.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yaml-only", action="store_true")
    args = parser.parse_args()
    configure_logging()
    init_sqlite_schema()
    settings = get_settings()
    with session_scope() as session:
        results: dict[str, int] = import_yaml_events(session, settings.labels_dir)
        if not args.yaml_only:
            results.update(import_gfw_events(session, settings))
    print(" ".join(f"{source}={count}" for source, count in sorted(results.items())))


if __name__ == "__main__":
    main()
