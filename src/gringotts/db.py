"""Engine, session factory, and declarative base.

`DATABASE_URL` selects the database (default: local SQLite file).
"""

import os
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# How long SQLite waits on a locked database before raising, in seconds.
SQLITE_BUSY_TIMEOUT = float(os.getenv("GRINGOTTS_SQLITE_BUSY_TIMEOUT", "30"))


class Base(DeclarativeBase):
    """Declarative base all gringotts models attach to."""


def make_engine(url: str) -> Engine:
    """Create an engine for `url`, applying SQLite-only tuning when needed.

    For SQLite the engine waits on a locked database instead of erroring
    ("database is locked") and uses WAL so a writer and readers can coexist —
    the configuration the concurrency test relies on, now shipped by default.
    """
    if not url.startswith("sqlite"):
        return create_engine(url)

    # check_same_thread is a SQLite-only flag; passing it to other drivers fails.
    # `timeout` sets the busy handler at the driver level; the PRAGMAs below make
    # WAL and busy_timeout stick on every pooled connection.
    engine = create_engine(
        url,
        connect_args={
            "check_same_thread": False,
            "timeout": SQLITE_BUSY_TIMEOUT,
        },
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial glue
        cursor = dbapi_conn.cursor()
        cursor.execute(f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT * 1000)}")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./gringotts.db")
engine = make_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a session that is always closed after use."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
