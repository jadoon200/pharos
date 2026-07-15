"""Real-data validation — run the full pipeline on a downloaded NOAA Marine Cadastre AIS slice.

The synthetic gold set is a known ceiling (self-generated anomalies are separable by construction);
the honest test is real AIS the model didn't get to define. This runs collect -> tracks -> the
detector ensemble -> optional GFW v3 corroboration -> the flagship GRU anomaly model over a real
NOAA CSV and reports the incident counts and the most-anomalous real tracks. There are no anomaly
*labels* in raw AIS, so the quantitative result is the false-positive behaviour in real
congested-port traffic; GFW supplies independent silver labels when its selected time/area window
has coverage, and the anomaly model's output is qualitative (interpretable outliers).

    # 1. download a real day (one national file, ~250 MB):
    #    curl -o data/ais/AIS_2020_01_01.zip \
    #      https://coast.noaa.gov/htdata/CMSP/AISDataHandler/2020/AIS_2020_01_01.zip
    # 2. filter to a region of interest with `python -m scripts.filter_noaa`, then:
    make eval-real FILE=data/ais/la_2020_01_01.csv REGION=us-la
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import timedelta

import numpy as np
from sqlalchemy import select

from pharos.config import get_settings
from pharos.db.base import init_sqlite_schema, session_scope
from pharos.db.models import Incident, Position, Track, Vessel
from pharos.detect.run import detect
from pharos.detect.seq_anomaly import SequenceAnomalyModel
from pharos.eval.gfw_check import corroborate
from pharos.ingest.gfw import GfwEvent, fetch_events
from pharos.ingest.noaa import load_csv
from pharos.ingest.persist import persist_scenario_or_positions
from pharos.ingest.reference import seed_zones
from pharos.logging import configure_logging, get_logger
from pharos.tracks.build import build_tracks

log = get_logger(__name__)


def _gfw_events_for_positions(
    positions: list[Position],
    bbox: tuple[float, float, float, float] | None = None,
) -> list[GfwEvent]:
    """Fetch independent GFW silver labels for the real AIS time/area window."""
    settings = get_settings()
    if not settings.gfw_token or not positions:
        return []
    start = min(p.ts for p in positions).date()
    # GFW endDate is exclusive, so include the final AIS day explicitly.
    end = max(p.ts for p in positions).date() + timedelta(days=1)
    if bbox is None:
        pad = 0.05
        bbox = (
            min(p.lat for p in positions) - pad,
            min(p.lon for p in positions) - pad,
            max(p.lat for p in positions) + pad,
            max(p.lon for p in positions) + pad,
        )
    events: list[GfwEvent] = []
    for detector in ("gap", "rendezvous", "loiter"):
        events.extend(fetch_events(detector, start.isoformat(), end.isoformat(), bbox=bbox))
    return events


def _print_gfw_cross_check(
    incidents: list[Incident],
    positions: list[Position],
    bbox: tuple[float, float, float, float] | None = None,
) -> None:
    """Run the optional real-label cross-check without making real-data eval depend on GFW."""
    if not get_settings().gfw_token:
        print("\nGFW cross-check: skipped (PHAROS_GFW_TOKEN not set)")
        return
    try:
        events = _gfw_events_for_positions(positions, bbox)
    except Exception as exc:  # integration boundary: the offline/NOAA lane must still complete
        log.warning("gfw_cross_check_failed", error=str(exc))
        print("\nGFW cross-check: unavailable (API request failed; NOAA evaluation continued)")
        return

    print(f"\nGFW cross-check: {len(events)} independent events in the AIS window")
    for agreement in corroborate(incidents, events):
        rate = "n/a" if agreement.rate != agreement.rate else f"{agreement.rate:.1%}"
        print(
            f"  {agreement.detector}: {agreement.corroborated}/{agreement.incidents} "
            f"PHAROS incidents corroborated ({rate})"
        )


def run(
    file: str,
    region: str,
    gfw_bbox: tuple[float, float, float, float] | None = None,
) -> None:
    init_sqlite_schema()
    vessels, positions = load_csv(file, region=region)
    print(f"REAL DATA: {len(vessels)} vessels, {len(positions):,} positions ({file})")
    with session_scope() as s:
        seed_zones(s)
        persist_scenario_or_positions(s, vessels, positions)
    with session_scope() as s:
        print("tracks:", build_tracks(s, region=region))
    with session_scope() as s:
        print("deterministic detectors:", detect(s, region=region))

    settings = get_settings()
    with session_scope() as s:
        incs = list(s.scalars(select(Incident).where(Incident.region == region)))
        print("\nincidents by detector:", dict(Counter(i.detector for i in incs)))
        _print_gfw_cross_check(incs, positions, gfw_bbox)

        # Flagship GRU on real pattern-of-life — flag the most unusual real tracks (qualitative).
        tracks = [t for t in s.scalars(select(Track).where(Track.region == region)) if t.sequence]
        if len(tracks) >= 8:
            x = np.array([t.sequence for t in tracks], dtype=np.float64)
            model = SequenceAnomalyModel(hidden=settings.anomaly_hidden, seed=0)
            hist = model.fit(x)
            scores = model.score(x)
            order = np.argsort(-scores)
            print(
                f"\nGRU-AE trained on {len(x)} real tracks "
                f"(val-loss best@{hist.best_epoch}, early-stopped@{hist.stopped_epoch})"
            )
            print("most anomalous real tracks:")
            for r in order[:6]:
                t = tracks[r]
                v = s.get(Vessel, t.mmsi)
                name = (v.name if v else None) or "?"
                print(
                    f"  score={scores[r]:8.2f}  mmsi={t.mmsi}  {name[:20]:20s} "
                    f"{t.point_count} pts, {t.distance_km} km"
                )


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", help="filtered NOAA CSV")
    parser.add_argument("region", nargs="?", default="us-la")
    parser.add_argument(
        "--gfw-bbox",
        nargs=4,
        type=float,
        metavar=("MIN_LAT", "MIN_LON", "MAX_LAT", "MAX_LON"),
        help="exact GFW query bbox; otherwise inferred from NOAA positions with a small pad",
    )
    args = parser.parse_args()
    bbox = tuple(args.gfw_bbox) if args.gfw_bbox else None
    run(args.file, args.region, bbox)


if __name__ == "__main__":
    main()
