"""Filter a NOAA Marine Cadastre national AIS ZIP into a compact bounding-box CSV.

The national daily archives are hundreds of megabytes and stay outside Git. This utility streams
the CSV member directly from the ZIP, preserves the NOAA header, and writes only reports inside the
requested ``(min_lat, min_lon, max_lat, max_lon)`` box.

    python -m scripts.filter_noaa data/ais/AIS_2023_07_25.zip \
        data/ais/gulf_2023_07_25.csv 27.0 -93.0 30.5 -88.0
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FilterResult:
    reports: int
    vessels: int
    sha256: str


def filter_zip(
    source: Path,
    output: Path,
    bbox: tuple[float, float, float, float],
    mmsi: set[str] | None = None,
) -> FilterResult:
    """Stream the sole NOAA CSV member in ``source`` into a bounding-box slice."""
    min_lat, min_lon, max_lat, max_lon = bbox
    if min_lat > max_lat or min_lon > max_lon:
        raise ValueError("bbox must be ordered as min_lat min_lon max_lat max_lon")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    reports = 0
    vessels: set[str] = set()

    with zipfile.ZipFile(source) as archive:
        members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"expected exactly one CSV member in {source}, found {len(members)}")
        with (
            archive.open(members[0]) as raw,
            io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as text,
            temporary.open("w", encoding="utf-8", newline="") as destination,
        ):
            reader = csv.DictReader(text)
            if reader.fieldnames is None or not {"MMSI", "LAT", "LON"}.issubset(reader.fieldnames):
                raise ValueError("NOAA CSV must contain MMSI, LAT, and LON columns")
            writer = csv.DictWriter(destination, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                if mmsi and row["MMSI"] not in mmsi:
                    continue
                try:
                    lat = float(row["LAT"])
                    lon = float(row["LON"])
                except (TypeError, ValueError):
                    continue
                if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
                    writer.writerow(row)
                    reports += 1
                    vessels.add(row["MMSI"])

    temporary.replace(output)
    with output.open("rb") as filtered:
        digest = hashlib.file_digest(filtered, "sha256").hexdigest()
    return FilterResult(reports=reports, vessels=len(vessels), sha256=digest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="NOAA national daily ZIP")
    parser.add_argument("output", type=Path, help="filtered CSV destination")
    parser.add_argument("min_lat", type=float)
    parser.add_argument("min_lon", type=float)
    parser.add_argument("max_lat", type=float)
    parser.add_argument("max_lon", type=float)
    parser.add_argument(
        "--mmsi",
        action="append",
        default=[],
        help="optional MMSI to retain (repeatable)",
    )
    args = parser.parse_args()
    result = filter_zip(
        args.source,
        args.output,
        (args.min_lat, args.min_lon, args.max_lat, args.max_lon),
        set(args.mmsi) or None,
    )
    print(
        f"filtered {result.reports:,} reports across {result.vessels:,} vessels; "
        f"sha256={result.sha256}"
    )


if __name__ == "__main__":
    main()
