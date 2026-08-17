"""gringotts migrate: brings a pre-balance_after database forward in place,
idempotently, and refuses to run on top of ledger drift."""

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from gringotts import crud, migrations
from gringotts.db import make_engine


def _legacy_db(tmp_path, credits=8):
    """A database shaped like pre-0.3.0 (full columns, no balance_after)."""
    engine = make_engine(f"sqlite:///{tmp_path}/legacy.db")
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE users ("
                "id INTEGER PRIMARY KEY, username VARCHAR NOT NULL, "
                "api_key_hash VARCHAR NOT NULL, key_last4 VARCHAR(4) DEFAULT '', "
                "credits INTEGER NOT NULL, is_admin BOOLEAN DEFAULT 0)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE credit_transactions ("
                "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
                "amount INTEGER NOT NULL, kind VARCHAR(16) NOT NULL, "
                "external_id VARCHAR, endpoint VARCHAR, amount_cents INTEGER, "
                "created_at DATETIME)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO users (id, username, api_key_hash, credits) "
                "VALUES (1, 'legacy', 'hash', :c)"
            ),
            {"c": credits},
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


def test_migrate_adds_payment_and_idempotency_schema(tmp_path):
    engine = _legacy_db(tmp_path)
    try:
        migrations.run_pending(engine)
        with engine.connect() as conn:
            insp = inspect(conn)
            cols = {c["name"] for c in insp.get_columns("credit_transactions")}
            tables = set(insp.get_table_names())
        assert "payment_intent_id" in cols
        assert "currency" in cols
        assert "balance_after" in cols
        # response-caching idempotency lives in its own table now
        assert "idempotency_records" in tables
        assert "checkout_orders" in tables
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


def test_reconcile_flags_null_balance_after(tmp_path):
    # On a migrated SQLite DB balance_after is nullable; a NULL in a non-latest
    # row would hide from the latest-row check, so reconcile must flag it.
    engine = _legacy_db(tmp_path)
    try:
        migrations.run_pending(engine)  # backfills [10, 8]
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE credit_transactions SET balance_after = NULL WHERE id = 1")
            )
        session = sessionmaker(bind=engine)()
        try:
            disc = crud.find_balance_discrepancies(session)
            assert len(disc) == 1
            assert disc[0]["null_balance_after_rows"] == 1
        finally:
            session.close()
    finally:
        engine.dispose()


def test_reconcile_clean_after_migrate(tmp_path):
    engine = _legacy_db(tmp_path)
    try:
        migrations.run_pending(engine)
        session = sessionmaker(bind=engine)()
        try:
            assert crud.find_balance_discrepancies(session) == []
        finally:
            session.close()
    finally:
        engine.dispose()


def test_legacy_event_keyed_purchases_detected(tmp_path):
    engine = _legacy_db(tmp_path)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO credit_transactions "
                    "(user_id, amount, kind, external_id) VALUES "
                    "(1, 100, 'purchase', 'evt_legacy'), "
                    "(1, 50, 'purchase', 'cs_modern')"
                )
            )
        assert migrations.legacy_event_keyed_purchases(engine) == 1  # only evt_ one
    finally:
        engine.dispose()


def test_fresh_db_migrate_then_noop(tmp_path):
    from gringotts.db import Base

    engine = make_engine(f"sqlite:///{tmp_path}/fresh.db")
    try:
        Base.metadata.create_all(bind=engine)  # fresh schema already has balance_after
        migrations.run_pending(engine)  # stamps head, no schema change needed
        assert migrations.run_pending(engine) == []  # second run is a no-op
    finally:
        engine.dispose()
