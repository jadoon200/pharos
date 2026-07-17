import asyncio
import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from pharos.collector.worker import CollectorWorker, PositionDownsampler, WebSocketClient
from pharos.config import Settings
from pharos.db.base import Base, make_engine
from pharos.db.models import CollectorRun, CoverageOutage, Position, Track, Vessel


def _position(ts: datetime, *, heading: float = 90.0, sog: float = 8.0) -> Position:
    return Position(
        mmsi="563123456",
        ts=ts,
        lat=1.25,
        lon=103.81,
        sog=sog,
        heading=heading,
        source="aisstream",
        region="singapore-live",
    )


def test_downsampler_preserves_material_changes() -> None:
    start = datetime(2026, 7, 16, 1, 0, tzinfo=UTC)
    sampler = PositionDownsampler(45.0, 20.0, 2.0)

    assert sampler.accept(_position(start))
    assert not sampler.accept(_position(start + timedelta(seconds=10)))
    assert sampler.accept(_position(start + timedelta(seconds=20), heading=115.0))
    assert sampler.accept(_position(start + timedelta(seconds=30), heading=115.0, sog=10.5))
    assert sampler.accept(_position(start + timedelta(seconds=80), heading=115.0, sog=10.5))


def _message(ts: str) -> str:
    return json.dumps(
        {
            "MessageType": "PositionReport",
            "MetaData": {
                "MMSI": 563123456,
                "ShipName": "LIVE VESSEL",
                "latitude": 1.25,
                "longitude": 103.81,
                "time_utc": ts,
            },
            "Message": {"PositionReport": {"Sog": 8.0, "Cog": 90.0, "TrueHeading": 90}},
        }
    )


class _FakeWebSocket:
    def __init__(
        self, messages: list[str], worker: CollectorWorker, *, stop_on_empty: bool = True
    ) -> None:
        self.messages = messages
        self.worker = worker
        self.stop_on_empty = stop_on_empty
        self.subscription = ""

    async def send(self, message: str) -> None:
        self.subscription = message

    async def recv(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        if self.stop_on_empty:
            self.worker.request_stop("test complete")
        await asyncio.Event().wait()
        return ""  # pragma: no cover - task is cancelled by the stop event


class _Connection(AbstractAsyncContextManager[WebSocketClient]):
    def __init__(self, websocket: WebSocketClient | None, *, fail: bool = False) -> None:
        self.websocket = websocket
        self.fail = fail

    async def __aenter__(self) -> WebSocketClient:
        if self.fail:
            raise OSError("simulated Wi-Fi loss")
        assert self.websocket is not None
        return self.websocket

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None


class _Connector:
    def __init__(self, websocket: WebSocketClient) -> None:
        self.websocket = websocket
        self.calls = 0

    def __call__(self, _url: str) -> AbstractAsyncContextManager[WebSocketClient]:
        self.calls += 1
        if self.calls == 1:
            return _Connection(None, fail=True)
        return _Connection(self.websocket)


class _SequenceConnector:
    def __init__(self, websockets: list[WebSocketClient]) -> None:
        self.websockets = websockets
        self.calls = 0

    def __call__(self, _url: str) -> AbstractAsyncContextManager[WebSocketClient]:
        websocket = self.websockets[self.calls]
        self.calls += 1
        return _Connection(websocket)


class _BatchBoundaryWebSocket:
    def __init__(self, worker: CollectorWorker) -> None:
        self.worker = worker
        self.call = 0

    async def send(self, _message: str) -> None:
        return None

    async def recv(self) -> str:
        self.call += 1
        if self.call == 1:
            return _message("2026-07-16 01:00:00.000000000 +0000 UTC")
        if self.call == 2:
            # Stay in flight across the first batch deadline (the flush happens around this
            # pending receive), then deliver the second batch's report.
            await asyncio.sleep(0.02)
            return _message("2026-07-16 01:01:00.000000000 +0000 UTC")
        self.worker.request_stop("two batches complete")
        await asyncio.Event().wait()
        return ""  # pragma: no cover - task is cancelled by the stop event


def test_worker_reconnects_records_outage_and_flushes_idempotently(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'collector.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        database_url=url,
        aisstream_key="test-key",
        collector_backoff_initial_seconds=0.001,
        collector_backoff_max_seconds=0.001,
        collector_batch_seconds=45.0,
        collector_downsample_seconds=45.0,
    )
    worker = CollectorWorker(settings, session_factory=factory, jitter=lambda: 0.0)
    websocket = _FakeWebSocket(
        [
            _message("2026-07-16 01:00:00.000000000 +0000 UTC"),
            _message("2026-07-16 01:00:00.000000000 +0000 UTC"),
        ],
        worker,
    )
    connector = _Connector(websocket)
    worker._connector = connector

    run_id = asyncio.run(worker.run())

    with Session(engine) as session:
        run = session.get(CollectorRun, run_id)
        assert run is not None
        assert run.status == "stopped"
        assert run.stop_reason == "test complete"
        assert run.report_count == 2
        assert run.vessel_count == 1
        assert session.scalar(select(func.count()).select_from(Position)) == 1
        outage = session.scalars(select(CoverageOutage)).one()
        assert outage.closed_at is not None
        assert "connection failure" in outage.reason
    subscription = json.loads(websocket.subscription)
    assert subscription["FilterMessageTypes"] == [
        "PositionReport",
        "StandardClassBPositionReport",
        "ShipStaticData",
    ]
    assert connector.calls == 2
    engine.dispose()


def test_restart_bridges_offline_window_as_outage(tmp_path: Path) -> None:
    """Downtime between runs (laptop off, launchd reload) must never become a vessel gap."""
    url = f"sqlite:///{tmp_path / 'restart-bridge.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    stopped_at = datetime(2026, 7, 16, 20, 0, tzinfo=UTC)
    with Session(engine) as session:
        session.add(
            CollectorRun(
                started_at=stopped_at - timedelta(hours=2),
                last_message_at=stopped_at - timedelta(minutes=1),
                stopped_at=stopped_at,
                stop_reason="requested",
                report_count=10,
                vessel_count=3,
                status="stopped",
            )
        )
        session.commit()
    settings = Settings(_env_file=None, database_url=url, aisstream_key="test-key")
    worker = CollectorWorker(settings, session_factory=factory)
    websocket = _FakeWebSocket([_message("2026-07-16 22:00:00.000000000 +0000 UTC")], worker)
    worker._connector = _SequenceConnector([websocket])

    run_id = asyncio.run(worker.run())

    with Session(engine) as session:
        outage = session.scalars(select(CoverageOutage)).one()
        assert outage.run_id == run_id
        assert outage.reason == "collector offline between runs"
        assert outage.opened_at.replace(tzinfo=UTC) == stopped_at
        # Closed by the first valid report of the new run, not left dangling.
        assert outage.closed_at is not None
        assert outage.closed_at >= outage.opened_at
    engine.dispose()


def test_worker_treats_feed_silence_as_coverage_outage(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'sleep-wake.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        database_url=url,
        aisstream_key="test-key",
        collector_health_timeout_seconds=0.01,
        collector_batch_seconds=1.0,
        collector_backoff_initial_seconds=0.001,
        collector_backoff_max_seconds=0.001,
    )
    worker = CollectorWorker(settings, session_factory=factory, jitter=lambda: 0.0)
    before_sleep = _FakeWebSocket(
        [_message("2026-07-16 01:00:00.000000000 +0000 UTC")],
        worker,
        stop_on_empty=False,
    )
    after_wake = _FakeWebSocket(
        [_message("2026-07-16 01:02:00.000000000 +0000 UTC")],
        worker,
    )
    connector = _SequenceConnector([before_sleep, after_wake])
    worker._connector = connector

    run_id = asyncio.run(worker.run())

    with Session(engine) as session:
        run = session.get(CollectorRun, run_id)
        assert run is not None and run.report_count == 2
        outage = session.scalars(select(CoverageOutage)).one()
        assert outage.reason == "valid report timeout"
        assert outage.closed_at is not None
        assert outage.closed_at >= outage.opened_at
        assert session.scalar(select(func.count()).select_from(Position)) == 2
    assert connector.calls == 2
    engine.dispose()


def test_worker_flushes_two_sqlite_batches_without_logging_key(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    url = f"sqlite:///{tmp_path / 'two-batches.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        database_url=url,
        aisstream_key="never-log-this-key",
        collector_batch_seconds=0.005,
        collector_health_timeout_seconds=1.0,
        collector_downsample_seconds=45.0,
    )
    worker = CollectorWorker(settings, session_factory=factory)
    websocket = _BatchBoundaryWebSocket(worker)
    worker._connector = _SequenceConnector([websocket])

    run_id = asyncio.run(worker.run())

    with Session(engine) as session:
        run = session.get(CollectorRun, run_id)
        assert run is not None and run.status == "stopped"
        assert run.report_count == 2
        assert session.scalar(select(func.count()).select_from(Position)) == 2
    captured = capsys.readouterr()
    assert "never-log-this-key" not in captured.out
    assert "never-log-this-key" not in captured.err
    engine.dispose()


def test_batch_flush_failure_retains_data_and_labels_outage(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A mid-run DB failure is not a 'connection failure', and the batch survives to retry."""
    url = f"sqlite:///{tmp_path / 'flush-retry.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        database_url=url,
        aisstream_key="test-key",
        collector_batch_seconds=0.005,
        collector_health_timeout_seconds=5.0,
        collector_backoff_initial_seconds=0.001,
        collector_backoff_max_seconds=0.001,
    )
    worker = CollectorWorker(settings, session_factory=factory, jitter=lambda: 0.0)

    from pharos.ingest import persist as persist_module

    real_ensure_vessels = persist_module.ensure_vessels
    failures = {"remaining": 1}

    def flaky_ensure_vessels(session: Session, vessels: list[Vessel]) -> int:
        if failures["remaining"]:
            failures["remaining"] -= 1
            raise TypeError("simulated transient DB failure")
        return real_ensure_vessels(session, vessels)

    monkeypatch.setattr("pharos.collector.worker.ensure_vessels", flaky_ensure_vessels)

    class _FlushRetryWebSocket:
        def __init__(self, worker: CollectorWorker) -> None:
            self.worker = worker
            self.call = 0

        async def send(self, _message: str) -> None:
            return None

        async def recv(self) -> str:
            self.call += 1
            if self.call == 1:
                return _message("2026-07-16 01:00:00.000000000 +0000 UTC")
            # Sit silent so the batch deadline fires (flush fails once → reconnect → retry).
            await asyncio.Event().wait()
            return ""  # pragma: no cover - task is cancelled

    class _StopAfterReconnectWebSocket:
        def __init__(self, worker: CollectorWorker) -> None:
            self.worker = worker

        async def send(self, _message: str) -> None:
            return None

        async def recv(self) -> str:
            self.worker.request_stop("retry verified")
            await asyncio.Event().wait()
            return ""  # pragma: no cover - task is cancelled

    worker._connector = _SequenceConnector(
        [_FlushRetryWebSocket(worker), _StopAfterReconnectWebSocket(worker)]
    )

    run_id = asyncio.run(worker.run())

    with Session(engine) as session:
        run = session.get(CollectorRun, run_id)
        assert run is not None and run.status == "stopped"
        # The failed batch was retained and persisted by the final flush.
        assert session.scalar(select(func.count()).select_from(Position)) == 1
        outage = session.scalars(select(CoverageOutage)).one()
        assert outage.reason == "batch persistence failure (TypeError)"
    engine.dispose()


def test_final_flush_failure_is_nonzero_and_redacts_key(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    url = f"sqlite:///{tmp_path / 'failed-flush.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        database_url=url,
        aisstream_key="also-never-log-this-key",
    )
    worker = CollectorWorker(settings, session_factory=factory)
    websocket = _FakeWebSocket(
        [_message("2026-07-16 01:00:00.000000000 +0000 UTC")],
        worker,
    )
    worker._connector = _SequenceConnector([websocket])

    def fail_persistence(_session: Session, _vessels: object) -> int:
        raise TypeError("simulated persistence failure")

    monkeypatch.setattr("pharos.collector.worker.ensure_vessels", fail_persistence)
    with pytest.raises(RuntimeError, match=r"collector final flush failed \(TypeError\)"):
        asyncio.run(worker.run())

    with Session(engine) as session:
        run = session.scalars(select(CollectorRun)).one()
        assert run.status == "failed"
        assert run.stop_reason == "final flush failure (TypeError)"
    captured = capsys.readouterr()
    assert "also-never-log-this-key" not in captured.out
    assert "also-never-log-this-key" not in captured.err
    engine.dispose()


def test_processing_cycle_clears_dirty_set_only_after_success(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'processing.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        database_url=url,
        aisstream_key="test-key",
        process_interval_minutes=0.0,
    )
    worker = CollectorWorker(settings, session_factory=factory)
    worker._dirty_mmsis = {"563123456"}
    processed: list[set[str]] = []

    def succeed(dirty: set[str]) -> dict[str, object]:
        processed.append(dirty)
        return {"ok": True}

    worker._process_dirty = succeed
    asyncio.run(worker._maybe_process())

    assert processed == [{"563123456"}]
    assert worker._dirty_mmsis == set()
    engine.dispose()


def test_processing_failure_retains_dirty_set(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'processing-failure.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        database_url=url,
        aisstream_key="test-key",
        process_interval_minutes=0.0,
    )
    worker = CollectorWorker(settings, session_factory=factory)
    worker._dirty_mmsis = {"563123456"}

    def fail(_dirty: set[str]) -> dict[str, object]:
        raise RuntimeError("simulated processing failure")

    worker._process_dirty = fail
    asyncio.run(worker._maybe_process())

    assert worker._dirty_mmsis == {"563123456"}
    engine.dispose()


def test_startup_recovers_unprocessed_dirty_backlog(tmp_path: Path) -> None:
    """A dirty set lost at stop/crash is re-derived from unprocessed position tails."""
    url = f"sqlite:///{tmp_path / 'dirty-backlog.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    ts = datetime(2026, 7, 16, 1, 0, tzinfo=UTC)

    def _report(mmsi: str, at: datetime) -> Position:
        return Position(
            mmsi=mmsi, ts=at, lat=1.25, lon=103.81, source="aisstream", region="singapore-live"
        )

    with Session(engine) as session:
        for mmsi in ("111111111", "222222222", "333333333"):
            session.add(Vessel(mmsi=mmsi))
            session.add(_report(mmsi, ts))
            session.add(_report(mmsi, ts + timedelta(minutes=5)))
        # 111…: fully processed (track covers its newest position). 222…: stale track.
        session.add(
            Track(
                track_id=f"111111111:{ts.isoformat()}",
                mmsi="111111111",
                region="singapore-live",
                start_ts=ts,
                end_ts=ts + timedelta(minutes=5),
                point_count=2,
            )
        )
        session.add(
            Track(
                track_id=f"222222222:{ts.isoformat()}",
                mmsi="222222222",
                region="singapore-live",
                start_ts=ts,
                end_ts=ts,
                point_count=1,
            )
        )
        session.commit()

    settings = Settings(_env_file=None, database_url=url, aisstream_key="test-key")
    worker = CollectorWorker(settings, session_factory=factory)
    recovered = worker._seed_dirty_backlog()

    assert recovered == 2
    assert worker._dirty_mmsis == {"222222222", "333333333"}
    engine.dispose()


def test_due_retention_prune_runs_off_event_loop(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'scheduled-prune.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        _env_file=None,
        database_url=url,
        aisstream_key="test-key",
        retention_prune_interval_hours=0.0,
    )
    worker = CollectorWorker(settings, session_factory=factory)
    calls = 0

    def prune() -> dict[str, int | float | str]:
        nonlocal calls
        calls += 1
        return {"deleted": 0}

    worker._prune = prune
    asyncio.run(worker._maybe_prune())

    assert calls == 1
    engine.dispose()
