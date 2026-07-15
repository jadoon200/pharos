"""Build a tractable, label-enriched NOAA cohort for GFW corroboration.

The cohort includes every vessel named by a GFW gap, encounter, or loiter event in the requested
time/area window that is also present in the NOAA slice, plus a deterministic background sample.
It is suitable for detector calibration and event matching, not for estimating event prevalence.

    python -m scripts.select_gfw_cohort data/ais/gulf_2023_07_25.csv \
        data/ais/gulf_gfw_cohort_2023_07_25.csv 2023-07-25 2023-07-26 \
        27.0 -93.0 30.5 -88.0 --background-vessels 150
"""

from __future__ import annotations

import argparse
import csv
import hashlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pharos.ingest.gfw import GfwEvent, fetch_events


@dataclass(frozen=True)
class CohortResult:
    gfw_events: int
    labeled_vessels: int
    matched_labeled_vessels: int
    background_vessels: int
    reports: int
    sha256: str


def event_mmsi(events: list[GfwEvent]) -> set[str]:
    """Primary and encounter-counterpart MMSIs named by GFW events."""
    values = {event.mmsi for event in events if event.mmsi}
    for event in events:
        counterpart = event.raw.get("encounter", {}).get("vessel", {}).get("ssvid")
        if counterpart:
            values.add(str(counterpart))
    return values


def _rank(mmsi: str) -> bytes:
    return hashlib.sha256(mmsi.encode()).digest()


def select_vessels(
    counts: Counter[str],
    labeled: set[str],
    background_vessels: int,
    min_reports: int = 6,
) -> tuple[set[str], set[str]]:
    """Return selected MMSIs and the subset carrying external labels."""
    eligible = {mmsi for mmsi, count in counts.items() if mmsi and count >= min_reports}
    matched_labeled = labeled & eligible
    background_pool = eligible - matched_labeled
    background = set(sorted(background_pool, key=_rank)[:background_vessels])
    return matched_labeled | background, matched_labeled


def build_cohort(
    source: Path,
    output: Path,
    events: list[GfwEvent],
    background_vessels: int,
) -> CohortResult:
    """Write all reports for externally labelled vessels plus deterministic background."""
    counts: Counter[str] = Counter()
    with source.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            counts[row.get("MMSI", "")] += 1

    labeled = event_mmsi(events)
    selected, matched_labeled = select_vessels(counts, labeled, background_vessels)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    reports = 0
    with (
        source.open(newline="", encoding="utf-8-sig") as source_handle,
        temporary.open("w", newline="", encoding="utf-8") as output_handle,
    ):
        reader = csv.DictReader(source_handle)
        if reader.fieldnames is None:
            raise ValueError("NOAA CSV has no header")
        writer = csv.DictWriter(output_handle, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            if row.get("MMSI") in selected:
                writer.writerow(row)
                reports += 1
    temporary.replace(output)
    with output.open("rb") as cohort:
        digest = hashlib.file_digest(cohort, "sha256").hexdigest()
    return CohortResult(
        gfw_events=len(events),
        labeled_vessels=len(labeled),
        matched_labeled_vessels=len(matched_labeled),
        background_vessels=len(selected - matched_labeled),
        reports=reports,
        sha256=digest,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="bounding-box NOAA CSV")
    parser.add_argument("output", type=Path, help="cohort CSV destination")
    parser.add_argument("start_date", help="GFW start date, inclusive")
    parser.add_argument("end_date", help="GFW end date, exclusive")
    parser.add_argument("min_lat", type=float)
    parser.add_argument("min_lon", type=float)
    parser.add_argument("max_lat", type=float)
    parser.add_argument("max_lon", type=float)
    parser.add_argument("--background-vessels", type=int, default=150)
    args = parser.parse_args()
    bbox = (args.min_lat, args.min_lon, args.max_lat, args.max_lon)
    events: list[GfwEvent] = []
    for detector in ("gap", "rendezvous", "loiter"):
        events.extend(fetch_events(detector, args.start_date, args.end_date, bbox=bbox))
    result = build_cohort(args.source, args.output, events, args.background_vessels)
    print(
        f"selected {result.matched_labeled_vessels:,}/{result.labeled_vessels:,} GFW-labelled "
        f"vessels from {result.gfw_events:,} events plus {result.background_vessels:,} background "
        f"vessels; {result.reports:,} reports; sha256={result.sha256}"
    )


if __name__ == "__main__":
    main()
