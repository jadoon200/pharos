"""Prefect ingestion flow — load a NOAA AIS slice into the corpus and seed the zones.

    python -m pharos.ingest.flows data/ais/AIS_2023_01_01_zone10.csv us-west

Seeds the reference zones, upserts a Vessel per MMSI, and inserts the *new* position reports
(re-running never duplicates a track). Prefect-free persistence lives in `persist.py`; this
module wires the Prefect flow and the CLI entrypoint.
"""

from __future__ import annotations

import sys

from prefect import flow, task

from pharos.db.base import session_scope
from pharos.db.models import Position, Vessel
from pharos.ingest.noaa import load_csv

# Re-exported so callers can persist without importing Prefect.
from pharos.ingest.persist import ensure_vessels, persist_positions
from pharos.ingest.reference import seed_zones
from pharos.logging import configure_logging, get_logger

log = get_logger(__name__)

__all__ = ["collect", "ensure_vessels", "ingest", "persist_positions", "seed_zones"]


@task
def collect(file: str, region: str | None) -> tuple[list[Vessel], list[Position]]:
    return load_csv(file, region)


@flow(name="pharos-ingest")
def ingest(file: str, region: str | None = None) -> dict[str, int]:
    vessels, positions = collect(file, region)
    with session_scope() as session:
        zones = seed_zones(session)
        n_vessels = ensure_vessels(session, vessels)
        stats = persist_positions(session, positions)
    stats["vessels"] = n_vessels
    stats["zones"] = zones
    log.info("ingest_complete", file=file, region=region, **stats)
    return stats


if __name__ == "__main__":
    configure_logging()
    if len(sys.argv) < 2 or not sys.argv[1]:
        raise SystemExit("usage: python -m pharos.ingest.flows <noaa_csv> [region]")
    file_arg = sys.argv[1]
    region_arg = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    ingest(file_arg, region_arg)
