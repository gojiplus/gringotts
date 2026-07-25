"""Engine, session factory, and declarative base.

`DATABASE_URL` selects the database (default: local SQLite file).
"""

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base all gringotts models attach to."""


def make_engine(url: str) -> Engine:
    """Create an engine for `url`, applying SQLite-only connect args when needed."""
    # check_same_thread is a SQLite-only flag; passing it to other drivers fails
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connect_args)


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
