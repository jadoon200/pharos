"""Shared observed-coverage interval math for labels, evaluation, health, and publication."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from pharos.db.models import CollectorRun, CoverageOutage, Track
from pharos.timeutil import utc_naive


@dataclass(frozen=True, order=True)
class TimeInterval:
    start: datetime
    end: datetime

    @property
    def hours(self) -> float:
        return max(0.0, (utc_naive(self.end) - utc_naive(self.start)).total_seconds() / 3600.0)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _merge(intervals: list[TimeInterval]) -> list[TimeInterval]:
    merged: list[TimeInterval] = []
    for interval in sorted(intervals, key=lambda item: utc_naive(item.start)):
        if utc_naive(interval.end) <= utc_naive(interval.start):
            continue
        if not merged or utc_naive(interval.start) > utc_naive(merged[-1].end):
            merged.append(interval)
            continue
        previous = merged[-1]
        merged[-1] = TimeInterval(previous.start, max(previous.end, interval.end))
    return merged


def _subtract(base: TimeInterval, cuts: list[TimeInterval]) -> list[TimeInterval]:
    fragments = [base]
    for cut in cuts:
        next_fragments: list[TimeInterval] = []
        for fragment in fragments:
            if utc_naive(cut.end) <= utc_naive(fragment.start) or utc_naive(cut.start) >= utc_naive(
                fragment.end
            ):
                next_fragments.append(fragment)
                continue
            if utc_naive(cut.start) > utc_naive(fragment.start):
                next_fragments.append(TimeInterval(fragment.start, min(cut.start, fragment.end)))
            if utc_naive(cut.end) < utc_naive(fragment.end):
                next_fragments.append(TimeInterval(max(cut.end, fragment.start), fragment.end))
        fragments = next_fragments
    return fragments


def observed_intervals(
    session: Session,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    now: datetime | None = None,
) -> list[TimeInterval]:
    """Return merged collector-run time minus every recorded outage.

    A running interval ends at its last valid source report, never wall-clock ``now``. This keeps
    feed silence and laptop sleep out of observed-time denominators even before an outage closes.
    """
    now = _as_utc(now or datetime.now(UTC))
    runs = session.scalars(select(CollectorRun).order_by(CollectorRun.started_at)).all()
    run_intervals: list[TimeInterval] = []
    for run in runs:
        end = run.stopped_at or run.last_message_at
        if end is None:
            continue
        start = _as_utc(run.started_at)
        stop = min(_as_utc(end), now)
        if start_at is not None:
            start = max(start, _as_utc(start_at))
        if end_at is not None:
            stop = min(stop, _as_utc(end_at))
        if utc_naive(stop) > utc_naive(start):
            run_intervals.append(TimeInterval(start, stop))

    outages = [
        TimeInterval(
            _as_utc(outage.opened_at),
            min(_as_utc(outage.closed_at) if outage.closed_at else now, now),
        )
        for outage in session.scalars(select(CoverageOutage)).all()
    ]
    cuts = _merge(outages)
    return _merge(
        [fragment for interval in _merge(run_intervals) for fragment in _subtract(interval, cuts)]
    )


def intersects_observed(start: datetime, end: datetime, intervals: list[TimeInterval]) -> bool:
    return any(
        utc_naive(start) <= utc_naive(interval.end) and utc_naive(end) >= utc_naive(interval.start)
        for interval in intervals
    )


def overlap_hours(start: datetime, end: datetime, intervals: list[TimeInterval]) -> float:
    seconds = 0.0
    for interval in intervals:
        left = max(_as_utc(start), interval.start)
        right = min(_as_utc(end), interval.end)
        seconds += max(0.0, (utc_naive(right) - utc_naive(left)).total_seconds())
    return seconds / 3600.0


def observed_hours(intervals: list[TimeInterval]) -> float:
    return sum(interval.hours for interval in intervals)


def observed_vessel_hours(session: Session, intervals: list[TimeInterval], region: str) -> float:
    """Sum track durations that fall in observed coverage, once per vessel-time track."""
    tracks = session.scalars(select(Track).where(Track.region == region)).all()
    return sum(overlap_hours(track.start_ts, track.end_ts, intervals) for track in tracks)
