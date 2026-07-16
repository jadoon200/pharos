from pathlib import Path

from pharos.collector.storage import sqlite_database_path, storage_status
from pharos.config import Settings


def test_storage_status_counts_sqlite_sidecars(tmp_path: Path) -> None:
    database = tmp_path / "live.db"
    database.write_bytes(b"x" * 10)
    Path(f"{database}-wal").write_bytes(b"x" * 20)
    Path(f"{database}-shm").write_bytes(b"x" * 30)
    gib = 1024**3
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{database}",
        storage_warn_gb=50 / gib,
        storage_hard_gb=100 / gib,
    )

    status = storage_status(settings)

    assert status.bytes_used == 60
    assert status.level == "warning"
    assert sqlite_database_path(settings.database_url) == database


def test_non_sqlite_storage_is_not_locally_capped() -> None:
    settings = Settings(_env_file=None)
    assert storage_status(settings).level == "normal"
    assert sqlite_database_path(settings.database_url) is None
