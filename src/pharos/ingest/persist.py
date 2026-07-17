"""Idempotent persistence for ingested AIS — Prefect-free.

Plain DB operations (not orchestration), so the slim API / demo-seed paths can persist without
pulling Prefect. `flows.py` re-exports these for the CLI flow. Vessels upsert by MMSI; positions
insert only if a report for the same (MMSI, timestamp) isn't already stored, so re-running an
ingest never duplicates a track.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from pharos.db.models import Position, Vessel
from pharos.timeutil import utc_aware, utc_naive


def _ts_key(ts: datetime) -> str:
    """A UTC-normalized timestamp key. SQLite drops tzinfo on round-trip, so a naive/aware
    mismatch would defeat (mmsi, ts) dedup — comparing on a UTC-naive ISO string is robust
    across the SQLite (naive) and Postgres (aware) dialects alike."""
    return utc_naive(ts).isoformat()


def merge_vessel_identity(target: Vessel, source: Vessel) -> None:
    """Fill `target`'s missing identity fields and widen first/last-seen from `source`."""
    target.name = target.name or source.name
    target.call_sign = target.call_sign or source.call_sign
    target.ship_type = target.ship_type or source.ship_type
    target.flag = target.flag or source.flag
    target.length = target.length or source.length
    target.width = target.width or source.width
    if source.first_seen and (
        target.first_seen is None or utc_naive(source.first_seen) < utc_naive(target.first_seen)
    ):
        target.first_seen = source.first_seen
    if source.last_seen and (
        target.last_seen is None or utc_naive(source.last_seen) > utc_naive(target.last_seen)
    ):
        target.last_seen = source.last_seen


def ensure_vessels(session: Session, vessels: list[Vessel]) -> int:
    """Upsert a Vessel row per MMSI (merge fills identity fields, widens first/last-seen)."""
    seen: set[str] = set()
    for v in vessels:
        if v.mmsi in seen:
            continue
        seen.add(v.mmsi)
        existing = session.get(Vessel, v.mmsi)
        if existing is None:
            session.add(v)
        else:
            merge_vessel_identity(existing, v)
    session.flush()  # satisfy the Position.mmsi FK before inserting positions
    return len(seen)


def persist_positions(session: Session, positions: list[Position]) -> dict[str, int]:
    """Insert new position reports only; skip any (MMSI, ts) already stored."""
    for p in positions:
        # Store UTC regardless of the producer's offset: SQLite persists raw clock fields, so
        # an aware non-UTC timestamp would otherwise be stored as its local clock time.
        p.ts = utc_aware(p.ts)
    incoming = {(p.mmsi, _ts_key(p.ts)): p for p in positions}  # de-dupe within the batch
    mmsis = {p.mmsi for p in positions}
    existing: set[tuple[str, str]] = set()
    # Load stored (mmsi, ts) for the incoming vessels and normalize in Python (see _ts_key).
    mmsi_list = list(mmsis)
    for i in range(0, len(mmsi_list), 500):
        chunk = mmsi_list[i : i + 500]
        rows = session.execute(
            select(Position.mmsi, Position.ts).where(Position.mmsi.in_(chunk))
        ).all()
        existing.update((m, _ts_key(t)) for m, t in rows)
    new = [p for k, p in incoming.items() if k not in existing]
    session.add_all(new)
    return {"new": len(new), "skipped": len(incoming) - len(new), "positions": len(incoming)}


def persist_scenario_or_positions(
    session: Session, vessels: list[Vessel], positions: list[Position]
) -> dict[str, int]:
    """Convenience: ensure vessels then persist positions in one call."""
    n_vessels = ensure_vessels(session, vessels)
    stats = persist_positions(session, positions)
    stats["vessels"] = n_vessels
    return stats
