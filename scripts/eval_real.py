"""Real-data validation — run the full pipeline on a downloaded NOAA Marine Cadastre AIS slice.

The synthetic gold set is a known ceiling (self-generated anomalies are separable by construction);
the honest test is real AIS the model didn't get to define. This runs collect -> tracks -> the
detector ensemble -> the flagship GRU anomaly model over a real NOAA CSV and reports the incident
counts and the most-anomalous real tracks. There are no anomaly *labels* in raw AIS (GFW would
provide silver labels), so the quantitative result is the false-positive behaviour in real
congested-port traffic; the anomaly model's output is qualitative (interpretable outliers).

    # 1. download a real day (one national file, ~250 MB):
    #    curl -o data/ais/AIS_2020_01_01.zip \
    #      https://coast.noaa.gov/htdata/CMSP/AISDataHandler/2020/AIS_2020_01_01.zip
    # 2. filter to a region of interest (see the bbox filter in the git history / README), then:
    make eval-real FILE=data/ais/la_2020_01_01.csv REGION=us-la
"""

from __future__ import annotations

import sys
from collections import Counter

import numpy as np
from sqlalchemy import select

from pharos.config import get_settings
from pharos.db.base import init_sqlite_schema, session_scope
from pharos.db.models import Incident, Track, Vessel
from pharos.detect.run import detect
from pharos.detect.seq_anomaly import SequenceAnomalyModel
from pharos.ingest.noaa import load_csv
from pharos.ingest.persist import persist_scenario_or_positions
from pharos.ingest.reference import seed_zones
from pharos.logging import configure_logging, get_logger
from pharos.tracks.build import build_tracks

log = get_logger(__name__)


def run(file: str, region: str) -> None:
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
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m scripts.eval_real <noaa_csv> [region]")
    run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "us-la")


if __name__ == "__main__":
    main()
