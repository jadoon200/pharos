"""Seed the curated maritime reference zones into the database.

The zones live in code (`pharos.zones`) as the system of record; this mirrors them into `Zone`
rows so the API can serve them as GeoJSON and incidents can foreign-key a `zone_id`. Idempotent:
re-running updates the geometry/metadata in place.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from pharos.db.models import Zone
from pharos.zones import all_zones


def seed_zones(session: Session) -> int:
    """Upsert a Zone row for every registry entry. Returns the count seeded."""
    zones = all_zones()
    for z in zones:
        session.merge(
            Zone(
                zone_id=z.zone_id,
                name=z.name,
                kind=z.kind,
                country=z.country,
                polygon=[[p[0], p[1]] for p in z.polygon],
                sensitive=1 if z.sensitive else 0,
                notes=z.notes,
            )
        )
    session.flush()
    return len(zones)
