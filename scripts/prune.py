"""CLI wrapper for WAL-safe live-position retention pruning."""

from __future__ import annotations

from pharos.collector.retention import prune_positions
from pharos.config import get_settings
from pharos.db.base import make_engine
from pharos.logging import configure_logging


def main() -> None:
    configure_logging()
    settings = get_settings()
    engine = make_engine(settings.database_url)
    try:
        prune_positions(engine, settings)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
