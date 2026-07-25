import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gringotts import db as gdb
from gringotts import models  # noqa: F401  (registers tables on Base)


@pytest.fixture
def db_session(monkeypatch):
    """Fresh database per test; gringotts.db.SessionLocal is patched so the
    library (dependencies, decorators, CLI) uses it too.

    Set GRINGOTTS_TEST_DATABASE_URL (e.g. a Postgres URL in CI) to run the
    suite against a real server instead of in-memory SQLite."""
    url = os.getenv("GRINGOTTS_TEST_DATABASE_URL")
    if url:
        engine = create_engine(url)
    else:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    gdb.Base.metadata.drop_all(bind=engine)
    gdb.Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(gdb, "SessionLocal", testing_session_local)

    session = testing_session_local()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
