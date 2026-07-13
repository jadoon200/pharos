from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from pharos.db.base import Base

# JSONB on Postgres, plain JSON elsewhere (keeps unit tests runnable on SQLite).
# none_as_null: Python None must mean SQL NULL, not a stored JSON `null` — so an explicit
# `= None` stays invisible to `IS NULL` filters and matched by `IS NOT NULL`.
JsonType = JSON(none_as_null=True).with_variant(postgresql.JSONB(none_as_null=True), "postgresql")


def _now() -> datetime:
    return datetime.now().astimezone()


class Vessel(Base):
    """A vessel keyed by its MMSI (Maritime Mobile Service Identity, the AIS station id).

    Identity fields are advisory: AIS is self-reported, so `name`/`flag`/`ship_type` can be
    absent or falsified — the spoofing detector exists precisely because they can't be
    trusted at face value.
    """

    __tablename__ = "vessels"

    mmsi: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    call_sign: Mapped[str | None] = mapped_column(String(32))
    ship_type: Mapped[str | None] = mapped_column(String(64))  # cargo|tanker|fishing|...
    flag: Mapped[str | None] = mapped_column(String(64))  # inferred from the MMSI MID
    length: Mapped[float | None] = mapped_column(Float())
    width: Mapped[float | None] = mapped_column(Float())
    first_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Position(Base):
    """A single AIS position report (one point on a vessel's track)."""

    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=True)
    mmsi: Mapped[str] = mapped_column(ForeignKey("vessels.mmsi"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    lat: Mapped[float] = mapped_column(Float())
    lon: Mapped[float] = mapped_column(Float())
    sog: Mapped[float | None] = mapped_column(Float())  # speed over ground (knots)
    cog: Mapped[float | None] = mapped_column(Float())  # course over ground (degrees)
    heading: Mapped[float | None] = mapped_column(Float())
    nav_status: Mapped[str | None] = mapped_column(String(64))
    # Provenance of the report — the AIS-reliability signal (terrestrial dense vs satellite
    # sparse coverage feeds the incident reliability grade). e.g. "noaa" | "aisstream".
    source: Mapped[str] = mapped_column(String(32), index=True, default="noaa")
    region: Mapped[str | None] = mapped_column(String(64), index=True)  # dataset slice label
    raw: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Track(Base):
    """A segmented voyage — a contiguous run of one vessel's positions between AIS gaps."""

    __tablename__ = "tracks"

    track_id: Mapped[str] = mapped_column(String(96), primary_key=True)  # "<mmsi>:<start_ts>"
    mmsi: Mapped[str] = mapped_column(ForeignKey("vessels.mmsi"), index=True)
    region: Mapped[str | None] = mapped_column(String(64), index=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    point_count: Mapped[int] = mapped_column(Integer(), default=0)
    distance_km: Mapped[float | None] = mapped_column(Float())
    start_lat: Mapped[float | None] = mapped_column(Float())
    start_lon: Mapped[float | None] = mapped_column(Float())
    end_lat: Mapped[float | None] = mapped_column(Float())
    end_lon: Mapped[float | None] = mapped_column(Float())
    # Cached fixed-length resampled feature vector for the anomaly model (list[float]);
    # stored in-DB so the SQLite path needs no vector extension.
    features: Mapped[list[float] | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Zone(Base):
    """A geospatial reference area — an EEZ, chokepoint, port, protected area, or lane.

    Geometry is a simple (lat, lon) ring in `polygon`; membership is tested with the pure
    point-in-polygon in `pharos.geo` (no PostGIS dependency). See `pharos.zones`.
    """

    __tablename__ = "zones"

    zone_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32), index=True)  # eez|chokepoint|port|protected|lane
    country: Mapped[str | None] = mapped_column(String(64))
    polygon: Mapped[list[list[float]]] = mapped_column(JsonType)  # [[lat, lon], ...]
    sensitive: Mapped[int] = mapped_column(Integer(), default=0)  # 1 = watch zone (weights risk)
    notes: Mapped[str | None] = mapped_column(Text())


class Incident(Base):
    """A detector output — a scored, source-rated maritime incident for human review.

    `reliability` is a NATO-Admiralty-style grade (A completely reliable … F cannot be
    judged) reflecting AIS confidence: a corroborated event in dense terrestrial coverage
    grades higher than a lone signal in sparse satellite coverage, where an apparent
    "dark ship" may just be a reception gap. An incident is decision support, NEVER an
    automated verdict of illicit activity.
    """

    __tablename__ = "incidents"

    incident_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    mmsi: Mapped[str] = mapped_column(ForeignKey("vessels.mmsi"), index=True)
    track_id: Mapped[str | None] = mapped_column(ForeignKey("tracks.track_id"))
    # detector: gap | rendezvous | loiter | spoof | anomaly
    detector: Mapped[str] = mapped_column(String(32), index=True)
    incident_type: Mapped[str] = mapped_column(String(64))  # human label, e.g. "dark ship"
    score: Mapped[float] = mapped_column(Float())  # detector confidence [0,1]
    severity: Mapped[str] = mapped_column(String(16), default="low")  # low|moderate|high|critical
    reliability: Mapped[str] = mapped_column(String(1), default="F")  # AIS-confidence grade A..F
    ts_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ts_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lat: Mapped[float | None] = mapped_column(Float())
    lon: Mapped[float | None] = mapped_column(Float())
    zone_id: Mapped[str | None] = mapped_column(ForeignKey("zones.zone_id"))
    counterpart_mmsi: Mapped[str | None] = mapped_column(String(16))  # the other ship in an STS
    # Maritime-threat tags (analogue of SENTINEL's ATT&CK techniques) + the detector's
    # transparent evidence (thresholds crossed, gap duration, displacement, etc.).
    techniques: Mapped[list[str] | None] = mapped_column(JsonType)
    evidence: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    region: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
