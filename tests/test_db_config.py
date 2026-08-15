"""The shipped SQLite engine must wait on locks (busy_timeout) and use WAL,
matching what the concurrency test relies on."""

from sqlalchemy import text

from gringotts.db import make_engine


def test_sqlite_engine_sets_busy_timeout_and_wal(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/t.db")
    try:
        with engine.connect() as conn:
            busy_timeout = conn.execute(text("PRAGMA busy_timeout")).scalar()
            journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        assert busy_timeout is not None
        assert busy_timeout > 0  # waits instead of erroring
        assert str(journal_mode).lower() == "wal"
    finally:
        engine.dispose()
