from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from pharos.config import get_settings


class Base(DeclarativeBase):
    pass


def _configure_sqlite_connection(dbapi_connection: Any, _connection_record: Any) -> None:
    """Apply the durability/concurrency policy to every SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def make_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    # A single-container SQLite deploy serves requests from a threadpool; allow cross-thread use.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


_session_factory: sessionmaker[Session] | None = None


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=make_engine(), expire_on_commit=False)
    return _session_factory


def init_sqlite_schema() -> None:
    """Create the schema directly for a SQLite database — no Alembic, no migration step.

    A free single-container deployment can run on a SQLite file (no Postgres server), and
    SQLite doesn't share Postgres's JSONB migrations. On a Postgres deployment this is a
    no-op (migrations own the schema). Idempotent: create_all only creates missing tables.
    """
    engine = make_engine()
    if engine.dialect.name != "sqlite":
        return
    from pharos.db import models  # noqa: F401 — register the mappers on Base.metadata

    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
