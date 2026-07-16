"""Local SQLite storage accounting and emergency sampling policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url

from pharos.config import Settings

_GIB = 1024**3


@dataclass(frozen=True)
class StorageStatus:
    bytes_used: int
    level: str  # normal | warning | hard

    @property
    def gib_used(self) -> float:
        return self.bytes_used / _GIB


def sqlite_database_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        return None
    return Path(url.database).expanduser().resolve()


def storage_status(settings: Settings) -> StorageStatus:
    path = sqlite_database_path(settings.database_url)
    if path is None:
        return StorageStatus(bytes_used=0, level="normal")
    bytes_used = sum(
        candidate.stat().st_size
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )
    gib_used = bytes_used / _GIB
    if gib_used >= settings.storage_hard_gb:
        level = "hard"
    elif gib_used >= settings.storage_warn_gb:
        level = "warning"
    else:
        level = "normal"
    return StorageStatus(bytes_used=bytes_used, level=level)
