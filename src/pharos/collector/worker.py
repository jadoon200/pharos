"""Long-running AISStream worker with bounded batches and an explicit coverage ledger."""

from __future__ import annotations

import asyncio
import json
import random
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractAsyncContextManager, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any, Protocol, cast

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pharos.collector.retention import prune_positions
from pharos.collector.storage import storage_status
from pharos.config import Settings, get_settings
from pharos.db.base import get_session_factory, init_sqlite_schema, make_engine
from pharos.db.models import CollectorRun, CoverageOutage, Position, Vessel
from pharos.ingest.aisstream import SUBSCRIPTION_MESSAGE_TYPES, parse_message
from pharos.ingest.persist import ensure_vessels, persist_positions
from pharos.logging import configure_logging, get_logger

log = get_logger(__name__)


class WebSocketClient(Protocol):
    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...


ConnectFactory = Callable[[str], AbstractAsyncContextManager[WebSocketClient]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _subscription_message(settings: Settings) -> str:
    """Serialize the credential-bearing payload without retaining it in the run-loop frame."""
    return json.dumps(
        {
            "APIKey": settings.aisstream_key,
            "BoundingBoxes": settings.aisstream_bbox,
            "FilterMessageTypes": list(SUBSCRIPTION_MESSAGE_TYPES),
        }
    )


def _heading_delta(left: float | None, right: float | None) -> float:
    if left is None or right is None:
        return 0.0
    return abs((left - right + 180.0) % 360.0 - 180.0)


@dataclass
class PositionDownsampler:
    """Keep routine samples sparse while retaining detector-relevant changes."""

    cadence_seconds: float
    heading_delta_degrees: float
    speed_delta_kn: float
    latest: dict[str, Position] = field(default_factory=dict)

    def accept(self, position: Position) -> bool:
        previous = self.latest.get(position.mmsi)
        if previous is None:
            self.latest[position.mmsi] = position
            return True

        elapsed = (position.ts - previous.ts).total_seconds()
        speed_changed = (
            previous.sog is not None
            and position.sog is not None
            and abs(position.sog - previous.sog) >= self.speed_delta_kn
        )
        nav_changed = previous.nav_status != position.nav_status and (
            previous.nav_status is not None or position.nav_status is not None
        )
        material_change = (
            _heading_delta(previous.heading, position.heading) >= self.heading_delta_degrees
            or speed_changed
            or nav_changed
        )
        if elapsed >= self.cadence_seconds or material_change:
            self.latest[position.mmsi] = position
            return True
        return False


def _merge_vessel(target: Vessel, source: Vessel) -> None:
    target.name = target.name or source.name
    target.call_sign = target.call_sign or source.call_sign
    target.ship_type = target.ship_type or source.ship_type
    target.flag = target.flag or source.flag
    target.length = target.length or source.length
    target.width = target.width or source.width
    if source.first_seen and (target.first_seen is None or source.first_seen < target.first_seen):
        target.first_seen = source.first_seen
    if source.last_seen and (target.last_seen is None or source.last_seen > target.last_seen):
        target.last_seen = source.last_seen


@dataclass
class CollectorBatch:
    vessels: dict[str, Vessel] = field(default_factory=dict)
    positions: list[Position] = field(default_factory=list)
    report_count: int = 0
    report_mmsis: set[str] = field(default_factory=set)
    last_message_at: datetime | None = None

    def add_vessel(self, vessel: Vessel) -> None:
        existing = self.vessels.get(vessel.mmsi)
        if existing is None:
            self.vessels[vessel.mmsi] = vessel
        else:
            _merge_vessel(existing, vessel)

    def add_report(self, vessel: Vessel, position: Position, *, store: bool) -> None:
        self.add_vessel(vessel)
        self.report_count += 1
        self.report_mmsis.add(position.mmsi)
        if self.last_message_at is None or position.ts > self.last_message_at:
            self.last_message_at = position.ts
        if store:
            self.positions.append(position)

    def clear(self) -> None:
        self.vessels.clear()
        self.positions.clear()
        self.report_count = 0
        self.report_mmsis.clear()
        self.last_message_at = None

    @property
    def empty(self) -> bool:
        return not self.vessels and self.report_count == 0


class ReceiveState(Enum):
    STOP = "stop"
    TIMEOUT = "timeout"


class FeedUnhealthy(RuntimeError):
    def __init__(self, opened_at: datetime) -> None:
        super().__init__("valid AIS report timeout")
        self.opened_at = opened_at


class CollectorWorker:
    """Collect continuously without treating host/feed downtime as vessel behaviour."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
        connector: ConnectFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = _utcnow,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.settings = settings or get_settings()
        self._session_factory = session_factory or get_session_factory()
        self._connector = connector
        self._monotonic = monotonic
        self._now = now
        self._jitter = jitter
        self._stop = asyncio.Event()
        self._stop_reason = "requested"
        self._run_id: int | None = None
        self._outage_open = False
        self._batch = CollectorBatch()
        self._dirty_mmsis: set[str] = set()
        self._next_process_at = self._monotonic() + self.settings.process_interval_minutes * 60.0
        self._next_prune_at = (
            self._monotonic() + self.settings.retention_prune_interval_hours * 3600.0
        )
        self._seen_mmsis: set[str] = set()
        self._connection_healthy = False
        self._storage_level = "normal"
        self._downsampler = PositionDownsampler(
            cadence_seconds=self.settings.collector_downsample_seconds,
            heading_delta_degrees=self.settings.collector_heading_delta_degrees,
            speed_delta_kn=self.settings.collector_speed_delta_kn,
        )

    @contextmanager
    def _session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def request_stop(self, reason: str = "requested") -> None:
        self._stop_reason = reason
        self._stop.set()

    def _start_run(self) -> int:
        started_at = self._now()
        with self._session() as session:
            stale_runs = session.scalars(
                select(CollectorRun).where(
                    CollectorRun.status == "running", CollectorRun.stopped_at.is_(None)
                )
            ).all()
            for stale in stale_runs:
                recovered_at = stale.last_message_at or stale.started_at
                stale.stopped_at = recovered_at
                stale.stop_reason = "recovered after unclean stop"
                stale.status = "interrupted"
                for outage in session.scalars(
                    select(CoverageOutage).where(
                        CoverageOutage.run_id == stale.id,
                        CoverageOutage.closed_at.is_(None),
                    )
                ):
                    outage.closed_at = recovered_at

            run = CollectorRun(
                started_at=started_at,
                report_count=0,
                vessel_count=0,
                status="running",
            )
            session.add(run)
            session.flush()
            run_id = run.id
        log.info("collector_run_started", run_id=run_id)
        return run_id

    def _open_outage(self, reason: str, opened_at: datetime | None = None) -> None:
        if self._run_id is None:
            return
        with self._session() as session:
            existing = session.scalar(
                select(CoverageOutage).where(
                    CoverageOutage.run_id == self._run_id,
                    CoverageOutage.closed_at.is_(None),
                )
            )
            if existing is not None:
                self._outage_open = True
                return
            outage = CoverageOutage(
                run_id=self._run_id,
                opened_at=opened_at or self._now(),
                reason=reason,
            )
            session.add(outage)
        self._outage_open = True
        log.warning("collector_outage_opened", run_id=self._run_id, reason=reason)

    def _close_outage(self, closed_at: datetime | None = None) -> None:
        if self._run_id is None or not self._outage_open:
            return
        closed = False
        with self._session() as session:
            outages = session.scalars(
                select(CoverageOutage).where(
                    CoverageOutage.run_id == self._run_id,
                    CoverageOutage.closed_at.is_(None),
                )
            ).all()
            for outage in outages:
                outage.closed_at = closed_at or self._now()
                closed = True
        if closed:
            log.info("collector_outage_closed", run_id=self._run_id)
        self._outage_open = False

    def _flush(self) -> dict[str, int]:
        if self._batch.empty or self._run_id is None:
            return {"new": 0, "skipped": 0, "positions": 0}
        stored_mmsis = {position.mmsi for position in self._batch.positions}
        with self._session() as session:
            ensure_vessels(session, list(self._batch.vessels.values()))
            stats = persist_positions(session, self._batch.positions)
            run = session.get(CollectorRun, self._run_id)
            if run is None:
                raise RuntimeError("collector run disappeared before batch flush")
            self._seen_mmsis.update(self._batch.report_mmsis)
            run.report_count += self._batch.report_count
            run.vessel_count = len(self._seen_mmsis)
            if self._batch.last_message_at is not None:
                run.last_message_at = self._batch.last_message_at
        report_count = self._batch.report_count
        self._batch.clear()
        self._dirty_mmsis.update(stored_mmsis)
        self._refresh_storage_policy()
        log.info("collector_batch_flushed", reports=report_count, **stats)
        return stats

    def _refresh_storage_policy(self) -> None:
        status = storage_status(self.settings)
        # At the hard cap, routine cadence samples are nonessential and stop; material
        # heading/speed/nav-status changes still pass PositionDownsampler.accept().
        self._downsampler.cadence_seconds = (
            float("inf") if status.level == "hard" else self.settings.collector_downsample_seconds
        )
        if status.level != self._storage_level:
            log_method = log.error if status.level == "hard" else log.warning
            if status.level == "normal":
                log_method = log.info
            log_method(
                "collector_storage_state",
                level=status.level,
                gib_used=round(status.gib_used, 3),
                routine_samples_enabled=status.level != "hard",
            )
            self._storage_level = status.level

    def _process_dirty(self, dirty_mmsis: set[str]) -> dict[str, object]:
        from pharos.tracks.incremental import process_dirty_vessels

        with self._session() as session:
            return process_dirty_vessels(
                session,
                dirty_mmsis,
                region=self.settings.collector_region,
                settings=self.settings,
            )

    async def _maybe_process(self) -> None:
        if self._monotonic() < self._next_process_at:
            return
        self._next_process_at = self._monotonic() + self.settings.process_interval_minutes * 60.0
        if not self._dirty_mmsis:
            return
        dirty = set(self._dirty_mmsis)
        try:
            stats = await asyncio.to_thread(self._process_dirty, dirty)
        except Exception as exc:
            # Collection remains primary: retain the dirty set and retry next cycle without
            # converting a processing/model failure into a feed outage.
            log.error(
                "collector_processing_failed",
                error_type=type(exc).__name__,
                dirty_vessels=len(dirty),
            )
            return
        self._dirty_mmsis.difference_update(dirty)
        log.info("collector_processing_complete", dirty_vessels=len(dirty), stats=stats)

    def _prune(self) -> dict[str, int | float | str]:
        engine = make_engine(self.settings.database_url)
        try:
            return prune_positions(engine, self.settings)
        finally:
            engine.dispose()

    async def _maybe_prune(self) -> None:
        if self._monotonic() < self._next_prune_at:
            return
        try:
            await asyncio.to_thread(self._prune)
        except Exception as exc:
            log.error("collector_prune_failed", error_type=type(exc).__name__)
            # A busy checkpoint is safe and expected occasionally; retry at the next batch.
            self._next_prune_at = self._monotonic() + self.settings.collector_batch_seconds
            return
        self._next_prune_at = (
            self._monotonic() + self.settings.retention_prune_interval_hours * 3600.0
        )

    def _finish_run(self, status: str, reason: str) -> None:
        if self._run_id is None:
            return
        stopped_at = self._now()
        self._close_outage(stopped_at)
        with self._session() as session:
            run = session.get(CollectorRun, self._run_id)
            if run is not None:
                run.stopped_at = stopped_at
                run.stop_reason = reason
                run.status = status
        log.info("collector_run_stopped", run_id=self._run_id, status=status, reason=reason)

    async def _recv_or_stop(
        self, websocket: WebSocketClient, timeout: float
    ) -> str | bytes | ReceiveState:
        receive_task = asyncio.create_task(websocket.recv())
        stop_task = asyncio.create_task(self._stop.wait())
        done, pending = await asyncio.wait(
            {receive_task, stop_task},
            timeout=max(0.0, timeout),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if not done:
            return ReceiveState.TIMEOUT
        if stop_task in done and stop_task.result():
            if receive_task in done:
                with suppress(Exception):
                    receive_task.result()
            return ReceiveState.STOP
        return receive_task.result()

    async def _consume(self, websocket: WebSocketClient) -> None:
        batch_deadline = self._monotonic() + self.settings.collector_batch_seconds
        last_valid_monotonic = self._monotonic()
        last_valid_wall = self._now()
        while not self._stop.is_set():
            current = self._monotonic()
            health_deadline = last_valid_monotonic + self.settings.collector_health_timeout_seconds
            timeout = min(batch_deadline, health_deadline) - current
            if timeout <= 0.0:
                if current >= health_deadline:
                    raise FeedUnhealthy(
                        last_valid_wall
                        + timedelta(seconds=self.settings.collector_health_timeout_seconds)
                    )
                self._flush()
                await self._maybe_process()
                await self._maybe_prune()
                batch_deadline = self._monotonic() + self.settings.collector_batch_seconds
                continue

            received = await self._recv_or_stop(websocket, timeout)
            if received is ReceiveState.STOP:
                return
            if received is ReceiveState.TIMEOUT:
                continue
            try:
                payload: Any = json.loads(received)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                log.warning("collector_message_skipped", reason="invalid JSON")
                continue
            if not isinstance(payload, dict):
                continue
            parsed = parse_message(payload)
            if parsed is None:
                continue
            vessel, position = parsed
            if position is None:
                self._batch.add_vessel(vessel)
                continue

            last_valid_monotonic = self._monotonic()
            last_valid_wall = self._now()
            self._connection_healthy = True
            self._close_outage(last_valid_wall)
            self._batch.add_report(
                vessel,
                position,
                store=self._downsampler.accept(position),
            )

    async def _wait_for_reconnect(self, seconds: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def run(self) -> int:
        """Run until requested to stop; return the durable collector-run id."""
        if not self.settings.aisstream_key:
            log.warning("collector_skip", reason="no PHAROS_AISSTREAM_KEY set")
            return 0
        connector = self._connector
        if connector is None:
            try:
                import websockets
            except ImportError:
                log.warning(
                    "collector_skip",
                    reason="websockets not installed (pip install .[live])",
                )
                return 0
            connector = cast(ConnectFactory, websockets.connect)

        self._run_id = self._start_run()
        delay = self.settings.collector_backoff_initial_seconds
        status = "stopped"
        reason = self._stop_reason
        try:
            while not self._stop.is_set():
                self._connection_healthy = False
                try:
                    async with connector(self.settings.aisstream_url) as websocket:
                        await websocket.send(_subscription_message(self.settings))
                        log.info("collector_connected", region=self.settings.collector_region)
                        await self._consume(websocket)
                except Exception as exc:
                    if self._stop.is_set():
                        break
                    opened_at = exc.opened_at if isinstance(exc, FeedUnhealthy) else self._now()
                    outage_reason = (
                        "valid report timeout"
                        if isinstance(exc, FeedUnhealthy)
                        else f"connection failure ({type(exc).__name__})"
                    )
                    self._open_outage(outage_reason, opened_at)
                    log.warning(
                        "collector_reconnecting",
                        reason=outage_reason,
                        backoff_seconds=round(delay, 2),
                    )
                    if self._connection_healthy:
                        delay = self.settings.collector_backoff_initial_seconds
                    wait_seconds = min(
                        self.settings.collector_backoff_max_seconds,
                        delay + delay * 0.1 * self._jitter(),
                    )
                    await self._wait_for_reconnect(wait_seconds)
                    delay = min(self.settings.collector_backoff_max_seconds, delay * 2.0)
            reason = self._stop_reason
        except Exception as exc:
            status = "failed"
            reason = f"worker failure ({type(exc).__name__})"
            raise
        finally:
            flush_error_type: str | None = None
            try:
                self._flush()
            except Exception as exc:
                status = "failed"
                flush_error_type = type(exc).__name__
                reason = f"final flush failure ({flush_error_type})"
                # Do not render a rich traceback here: coroutine frames can contain the
                # credential-bearing subscription payload. The exception class is sufficient
                # operationally and cannot leak the API key.
                log.error("collector_final_flush_failed", error_type=flush_error_type)
            self._finish_run(status, reason)
            if flush_error_type is not None:
                raise RuntimeError(f"collector final flush failed ({flush_error_type})") from None
        return self._run_id


async def _run_worker() -> None:
    init_sqlite_schema()
    worker = CollectorWorker()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):  # macOS/Linux support signal handlers
            loop.add_signal_handler(signum, worker.request_stop, signum.name.lower())
    try:
        await worker.run()
    except Exception as exc:
        # Keep launchd output secret-safe and exit non-zero so restart-on-failure applies.
        log.error("collector_worker_failed", error_type=type(exc).__name__)
        raise SystemExit(1) from None


def main() -> None:
    configure_logging()
    asyncio.run(_run_worker())


if __name__ == "__main__":
    main()
