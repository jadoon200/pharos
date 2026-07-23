"""Measure the empirical coverage model on real NOAA AIS — the dark-ship calibration.

    python -m scripts.eval_coverage data/ais/gulf_2023_07_25.csv

Loads a real NOAA slice, runs the gap detector twice (with and without the coverage model),
and reports how the calls redistribute across the three verdicts. This is the answer to the
open negative in `docs/EVAL.md`: GFW's gap labels all sit offshore of NOAA's terrestrial
footprint, so external calibration was impossible — the corpus is used to calibrate itself
instead.

Optionally pass `--mmsi` to inspect specific vessels (e.g. the GFW-labelled offshore gap
vessel, which must come out coverage-explained: NOAA demonstrably cannot hear out there).
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime
from pathlib import Path

from pharos.config import Settings
from pharos.db.models import Position
from pharos.detect.coverage import CoverageModel
from pharos.detect.gaps import detect_gaps
from pharos.logging import configure_logging, get_logger

log = get_logger(__name__)


def load_positions(path: Path, limit: int | None = None) -> list[Position]:
    """Stream a NOAA Marine Cadastre CSV into Position rows (not persisted)."""
    out: list[Position] = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                out.append(
                    Position(
                        mmsi=row["MMSI"],
                        ts=datetime.fromisoformat(row["BaseDateTime"]),
                        lat=float(row["LAT"]),
                        lon=float(row["LON"]),
                        sog=float(row["SOG"]) if row.get("SOG") else None,
                        region="us-gulf",
                    )
                )
            except (KeyError, ValueError):
                continue
            if limit and len(out) >= limit:
                break
    return out


def footprint_profile(
    positions: list[Position], band_deg: float = 0.5
) -> list[tuple[float, int, int]]:
    """Distinct vessels heard per latitude band — the receiver footprint's offshore falloff.

    In the Gulf corpus the coast runs along the north of the box, so decreasing latitude is
    increasing distance from shore. Cell counts are reported alongside so the falloff can be
    read as reception thinning rather than a difference in surveyed area.
    """
    vessels: dict[float, set[str]] = {}
    cells: dict[float, set[tuple[int, int]]] = {}
    for p in positions:
        band = (p.lat // band_deg) * band_deg
        vessels.setdefault(band, set()).add(p.mmsi)
        cells.setdefault(band, set()).add((int(p.lat // 0.25), int(p.lon // 0.25)))
    return [(b, len(vessels[b]), len(cells[b])) for b in sorted(vessels, reverse=True)]


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Calibrate dark-ship gaps on real AIS")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--mmsi", nargs="*", default=[], help="vessels to inspect individually")
    parser.add_argument("--cell-deg", type=float, default=0.25)
    parser.add_argument("--min-witnesses", type=int, default=2)
    parser.add_argument(
        "--profile", action="store_true", help="report the receiver footprint by latitude band"
    )
    args = parser.parse_args()

    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    positions = load_positions(args.csv, args.limit)
    vessels = len({p.mmsi for p in positions})
    log.info("loaded", reports=len(positions), vessels=vessels)

    model = CoverageModel.from_positions(
        positions, cell_deg=args.cell_deg, min_witnesses=args.min_witnesses
    )
    baseline = detect_gaps(positions, settings)
    graded = detect_gaps(positions, settings, coverage=model)

    verdicts = Counter(str(i.evidence["coverage_verdict"]) for i in graded)
    base_grades = Counter(i.reliability for i in baseline)
    new_grades = Counter(i.reliability for i in graded)

    print(f"\ncorpus: {len(positions):,} reports / {vessels:,} vessels")
    print(f"gap calls: {len(graded)}  (identical signal; only the grading changed)\n")
    print("coverage verdict distribution")
    for verdict in ("vessel-attributed", "partial", "coverage-explained"):
        n = verdicts.get(verdict, 0)
        pct = 100.0 * n / len(graded) if graded else 0.0
        print(f"  {verdict:<20} {n:>4}  ({pct:5.1f}%)")

    print("\nAdmiralty reliability, before -> after")
    for grade in "BCDEF":
        print(f"  {grade}: {base_grades.get(grade, 0):>4} -> {new_grades.get(grade, 0):>4}")

    if args.profile:
        print("\nreceiver footprint by latitude band (north = coastal, south = offshore)")
        print(f"  {'lat band':>9} {'vessels':>8} {'cells':>6}")
        for band, n_vessels, n_cells in footprint_profile(positions):
            print(f"  {band:>9.1f} {n_vessels:>8} {n_cells:>6}")

    for mmsi in args.mmsi:
        calls = [i for i in graded if i.mmsi == mmsi]
        print(f"\n--- {mmsi}: {len(calls)} gap call(s) ---")
        for i in calls:
            ev = i.evidence
            print(
                f"  {i.ts_start} -> {i.ts_end} | {ev['gap_minutes']} min, "
                f"{ev['displacement_km']} km | {ev['coverage_verdict']} "
                f"(support {ev['corridor_support']}, {ev['witness_vessels']} witnesses) "
                f"| grade {i.reliability}"
            )


if __name__ == "__main__":
    main()
