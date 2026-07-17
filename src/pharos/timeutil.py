"""Timezone-normalization helpers shared across storage dialects.

SQLite round-trips `DateTime(timezone=True)` as naive clock fields while Postgres returns aware
UTC, so any code comparing stored and in-memory timestamps must normalize both sides first.
Naive values are by project convention already UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_naive(value: datetime) -> datetime:
    """Comparable UTC timestamp for dialects (notably SQLite) that drop tzinfo."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def utc_aware(value: datetime) -> datetime:
    """Normalize to aware UTC (naive input is by convention already UTC)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
