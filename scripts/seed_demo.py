"""Build a self-contained demo database for the API + dashboard (and the free deploy seed).

Runs the whole pipeline over the deterministic synthetic gold scenario — zones, positions, tracks,
the full detector ensemble + the anomaly model — into a SQLite file, so the API serves a rich
maritime picture with no Postgres and no live fetch. Also the M9 baked seed.

    PHAROS_DATABASE_URL=sqlite:///data/demo.db python -m scripts.seed_demo
"""

from __future__ import annotations

import sys

from pharos.db.base import init_sqlite_schema, session_scope
from pharos.detect.ensemble import run_all, vessel_rollups
from pharos.ingest.persist import persist_scenario_or_positions
from pharos.ingest.reference import seed_zones
from pharos.ingest.synthetic import generate_scenario
from pharos.logging import configure_logging, get_logger
from pharos.tracks.build import build_tracks

log = get_logger(__name__)


def seed(region: str = "singapore", n_normal: int = 18) -> dict[str, int]:
    init_sqlite_schema()
    scenario = generate_scenario(region, seed=0, n_normal=n_normal)
    with session_scope() as session:
        zones = seed_zones(session)
        stats = persist_scenario_or_positions(session, scenario.vessels, scenario.positions)
    with session_scope() as session:
        build_tracks(session, region=region)
    with session_scope() as session:
        run_all(session, region=region)
    with session_scope() as session:
        rollups = vessel_rollups(session, region=region)
    log.info("seed_complete", zones=zones, threats=len(rollups), **stats)
    return {"zones": zones, "threats": len(rollups), **stats}


def main() -> None:
    configure_logging()
    region = sys.argv[1] if len(sys.argv) > 1 else "singapore"
    result = seed(region)
    print(result)


if __name__ == "__main__":
    main()
