"""NOAA Marine Cadastre AIS CSV loader — the free, keyless, reproducible data workhorse.

NOAA publishes bulk historical AIS as daily CSVs (one file per day, sliceable by zone) at
https://marinecadastre.gov/ais/ — millions of real position reports, no API key. This module
parses one such CSV into `Vessel` + `Position` ORM rows. It is tolerant of header-casing and a
couple of documented schema revisions; unknown/blank fields degrade to None rather than failing.

The daily files are large (hundreds of MB) — never committed to git (see `.gitignore`). The
repo instead ships `pharos.ingest.synthetic` (deterministic, labelled) for tests, the demo seed,
and the eval gold set; this loader is the path for real NOAA data the user downloads locally.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pharos.db.models import Position, Vessel
from pharos.logging import get_logger

log = get_logger(__name__)

# NOAA AIS CSV columns (2018+ schema), mapped case-insensitively in parse_rows:
#   MMSI,BaseDateTime,LAT,LON,SOG,COG,Heading,VesselName,IMO,CallSign,VesselType,
#   Status,Length,Width,Draft,Cargo,TransceiverClass


def _f(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_ts(value: str) -> datetime | None:
    """NOAA timestamps are ISO-8601 without a zone, e.g. 2023-01-01T00:00:03."""
    value = value.strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            # Marine Cadastre BaseDateTime is UTC but carries no explicit offset. Calling
            # astimezone() on a naive value would incorrectly interpret it in the workstation's
            # local timezone before conversion.
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


# Ship-type code → coarse category. NOAA VesselType follows the ITU AIS type codes.
def ship_type_label(code: float | None) -> str | None:
    if code is None:
        return None
    c = int(code)
    if 30 <= c <= 39:
        return "fishing" if c == 30 else "special"
    if 60 <= c <= 69:
        return "passenger"
    if 70 <= c <= 79:
        return "cargo"
    if 80 <= c <= 89:
        return "tanker"
    if c in (31, 32, 52):
        return "tug"
    return "other"


def parse_rows(
    rows: Iterator[dict[str, str]], region: str | None
) -> tuple[dict[str, Vessel], list[Position]]:
    """Parse NOAA AIS dict-rows into de-duplicated Vessels + a list of Positions.

    Vessels are keyed by MMSI (identity fields filled from the first non-empty sighting).
    Rows without a usable MMSI / lat / lon / timestamp are skipped.
    """
    vessels: dict[str, Vessel] = {}
    positions: list[Position] = []
    for raw in rows:
        row = {k.strip().lower(): v for k, v in raw.items()}
        mmsi = (row.get("mmsi") or "").strip()
        ts = _parse_ts(row.get("basedatetime", ""))
        lat, lon = _f(row.get("lat")), _f(row.get("lon"))
        if not mmsi or ts is None or lat is None or lon is None:
            continue
        if mmsi not in vessels:
            vessels[mmsi] = Vessel(
                mmsi=mmsi,
                name=(row.get("vesselname") or None) or None,
                call_sign=(row.get("callsign") or None) or None,
                ship_type=ship_type_label(_f(row.get("vesseltype"))),
                flag=flag_for_mmsi(mmsi),
                length=_f(row.get("length")),
                width=_f(row.get("width")),
                first_seen=ts,
                last_seen=ts,
            )
        else:
            v = vessels[mmsi]
            if v.first_seen is None or ts < v.first_seen:
                v.first_seen = ts
            if v.last_seen is None or ts > v.last_seen:
                v.last_seen = ts
        positions.append(
            Position(
                mmsi=mmsi,
                ts=ts,
                lat=lat,
                lon=lon,
                sog=_f(row.get("sog")),
                cog=_f(row.get("cog")),
                heading=_f(row.get("heading")),
                nav_status=(row.get("status") or None) or None,
                source="noaa",
                region=region,
            )
        )
    return vessels, positions


def load_csv(path: str | Path, region: str | None = None) -> tuple[list[Vessel], list[Position]]:
    """Load a NOAA Marine Cadastre AIS CSV into Vessel + Position ORM objects."""
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        vessels, positions = parse_rows(csv.DictReader(fh), region)
    log.info("noaa_loaded", file=str(path), vessels=len(vessels), positions=len(positions))
    return list(vessels.values()), positions


# --- MMSI → flag state (first three digits are the Maritime Identification Digits) --------
# A small MID→country map covering the regions PHAROS focuses on; unknown → None. AIS MMSIs
# are self-reported, so this is advisory provenance, not ground truth.
_MID: dict[str, str] = {
    "563": "SG",
    "564": "SG",
    "565": "SG",
    "566": "SG",  # Singapore
    "525": "ID",
    "533": "MY",
    "567": "TH",
    "574": "VN",  # SE Asia
    "412": "CN",
    "413": "CN",
    "414": "CN",  # China
    "431": "JP",
    "432": "JP",
    "440": "KR",
    "441": "KR",  # Japan / Korea
    "366": "US",
    "367": "US",
    "368": "US",
    "369": "US",
    "338": "US",  # United States
    "273": "RU",  # Russia
    "422": "IR",  # Iran
}


def flag_for_mmsi(mmsi: str) -> str | None:
    mmsi = mmsi.strip()
    if len(mmsi) < 3 or not mmsi[:3].isdigit():
        return None
    return _MID.get(mmsi[:3])


def as_records(positions: list[Position]) -> list[dict[str, Any]]:
    """Positions → plain dicts (for logging / debugging / the synthetic round-trip test)."""
    return [
        {"mmsi": p.mmsi, "ts": p.ts.isoformat(), "lat": p.lat, "lon": p.lon, "sog": p.sog}
        for p in positions
    ]
