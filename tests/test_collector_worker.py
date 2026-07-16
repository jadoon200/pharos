import asyncio
import json
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from pharos.collector.worker import CollectorWorker, PositionDownsampler, WebSocketClient
from pharos.config import Settings
from pharos.db.base import Base, make_engine
from pharos.db.models import CollectorRun, CoverageOutage, Position


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
