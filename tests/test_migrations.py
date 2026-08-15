"""gringotts migrate: brings a pre-balance_after database forward in place,
idempotently, and refuses to run on top of ledger drift."""

import pytest
from sqlalchemy import text

from gringotts import migrations
from gringotts.db import make_engine


def _legacy_db(tmp_path, credits=8):
    """A database shaped like pre-0.3.0: no balance_after column."""
    engine = make_engine(f"sqlite:///{tmp_path}/legacy.db")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE users (id INTEGER PRIMARY KEY, credits INTEGER NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE credit_transactions ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                "amount INTEGER NOT NULL, kind VARCHAR(16) NOT NULL)"
            )
        )
        conn.execute(
            text("INSERT INTO users (id, credits) VALUES (1, :c)"), {"c": credits}
        )
        conn.execute(
            text(
                "INSERT INTO credit_transactions (user_id, amount, kind) VALUES "
                "(1, 10, 'grant'), (1, -2, 'charge')"
            )
        )
    return engine


def _balance_afters(engine):
    with engine.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                text("SELECT balance_after FROM credit_transactions ORDER BY id")
            )
        ]


def test_migrate_backfills_balance_after(tmp_path):
    engine = _legacy_db(tmp_path)
    try:
        applied = migrations.run_pending(engine)
        assert applied  # something ran
        assert _balance_afters(engine) == [10, 8]  # running balance
        with engine.connect() as conn:
            version = conn.execute(
                text("SELECT version FROM gringotts_schema_version")
            ).scalar()
        assert version == migrations.HEAD
    finally:
        engine.dispose()


def test_migrate_is_idempotent(tmp_path):
    engine = _legacy_db(tmp_path)
    try:
        migrations.run_pending(engine)
        again = migrations.run_pending(engine)
        assert again == []  # nothing left to do
        assert _balance_afters(engine) == [10, 8]
    finally:
        engine.dispose()


def test_migrate_refuses_on_drift(tmp_path):
    engine = _legacy_db(tmp_path, credits=999)  # credits != SUM(amount)=8
    try:
        with pytest.raises(RuntimeError, match="reconcile"):
            migrations.run_pending(engine)
    finally:
        engine.dispose()


def test_stamp_head_makes_migrate_a_noop(tmp_path):
    from gringotts.db import Base

    engine = make_engine(f"sqlite:///{tmp_path}/fresh.db")
    try:
        Base.metadata.create_all(bind=engine)  # fresh schema already has balance_after
        migrations.stamp_head(engine)
        assert migrations.run_pending(engine) == []
    finally:
        engine.dispose()
