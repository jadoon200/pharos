from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from pharos.db import models  # noqa: F401  (register tables on Base.metadata)
from pharos.db.base import Base


@pytest.fixture
def session() -> Iterator[Session]:
    """An isolated in-memory SQLite session with the full schema created.

    Models use a JSON/JSONB variant so the schema builds on SQLite with no Postgres —
    mirrors SENTINEL/ARGUS's test DB approach.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
