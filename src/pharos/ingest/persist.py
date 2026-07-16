"""Idempotent persistence for ingested AIS — Prefect-free.

Plain DB operations (not orchestration), so the slim API / demo-seed paths can persist without
pulling Prefect. `flows.py` re-exports these for the CLI flow. Vessels upsert by MMSI; positions
insert only if a report for the same (MMSI, timestamp) isn't already stored, so re-running an
ingest never duplicates a track.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from pharos.db.models import Position, Vessel


def _ts_key(ts: datetime) -> str:
    """A tz-normalized timestamp key. SQLite drops tzinfo on round-trip, so a naive/aware
    mismatch would defeat (mmsi, ts) dedup — comparing on a normalized ISO string is robust
    across the SQLite (naive) and Postgres (aware) dialects alike."""
    return (ts.replace(tzinfo=None) if ts.tzinfo else ts).isoformat()


def _utc_naive(ts: datetime) -> datetime:
    """Comparable UTC timestamp for dialects (notably SQLite) that drop tzinfo."""
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


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
            existing.name = existing.name or v.name
            existing.call_sign = existing.call_sign or v.call_sign
            existing.ship_type = existing.ship_type or v.ship_type
            existing.flag = existing.flag or v.flag
            existing.length = existing.length or v.length
            existing.width = existing.width or v.width
            if v.first_seen and (
                existing.first_seen is None
                or _utc_naive(v.first_seen) < _utc_naive(existing.first_seen)
            ):
                existing.first_seen = v.first_seen
            if v.last_seen and (
                existing.last_seen is None
                or _utc_naive(v.last_seen) > _utc_naive(existing.last_seen)
            ):
                existing.last_seen = v.last_seen
    session.flush()  # satisfy the Position.mmsi FK before inserting positions
    return len(seen)


def persist_positions(session: Session, positions: list[Position]) -> dict[str, int]:
    """Insert new position reports only; skip any (MMSI, ts) already stored."""
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
