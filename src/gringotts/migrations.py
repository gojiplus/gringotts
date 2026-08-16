"""Forward-only, idempotent schema migrations run by ``gringotts migrate``.

A ``gringotts_schema_version`` row records how far a database has been migrated.
Steps introspect the live schema, so re-running is safe and a database created
before this framework existed is brought forward without data loss. This owns
every additive change from 0.2.0 onward (new columns, indexes, backfills); the
one legacy hop that needs a full table rebuild — adding ``CHECK(credits>=0)`` to
a pre-0.2.0 SQLite ``users`` table — is a documented recreate, not a step here.
"""

from collections.abc import Callable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

SCHEMA_VERSION_TABLE = "gringotts_schema_version"


def _has_column(conn, table: str, column: str) -> bool:
    return any(c["name"] == column for c in inspect(conn).get_columns(table))


# The table name is a fixed module constant, never user input — the S608
# SQL-injection warnings on these internal DDL/DML strings are false positives.
def _ensure_version_row(conn) -> int:
    conn.execute(
        text(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} "
            "(version INTEGER NOT NULL)"
        )
    )
    row = conn.execute(
        text(f"SELECT version FROM {SCHEMA_VERSION_TABLE}")  # noqa: S608
    ).first()
    if row is None:
        conn.execute(
            text(f"INSERT INTO {SCHEMA_VERSION_TABLE} (version) VALUES (0)")  # noqa: S608
        )
        return 0
    return int(row[0])


def _set_version(conn, version: int) -> None:
    conn.execute(
        text(f"UPDATE {SCHEMA_VERSION_TABLE} SET version = :v"),  # noqa: S608
        {"v": version},
    )


def _ledger_drift(conn) -> list[tuple[int, int, int]]:
    """Report users whose cached credits disagree with SUM(amount).

    Runs before ``balance_after`` may exist, so it cannot use the three-way
    reconcile; this two-way check is what gates a migration.
    """
    result = conn.execute(
        text(
            "SELECT u.id, u.credits, COALESCE(SUM(t.amount), 0) AS ledger "
            "FROM users u LEFT JOIN credit_transactions t ON t.user_id = u.id "
            "GROUP BY u.id, u.credits"
        )
    )
    return [(r[0], r[1], int(r[2])) for r in result if r[1] != int(r[2])]


# --- migration steps: (target_version, description, fn(conn)) ---------------


def _add_balance_after(conn) -> None:
    dialect = conn.engine.dialect.name
    if not _has_column(conn, "credit_transactions", "balance_after"):
        # SQLite (>=3.37) and Postgres both allow an inline CHECK on ADD COLUMN.
        conn.execute(
            text(
                "ALTER TABLE credit_transactions "
                "ADD COLUMN balance_after INTEGER CHECK (balance_after >= 0)"
            )
        )
    # Backfill the running balance per user, oldest row first. Idempotent: only
    # touches rows still NULL, so re-running (or a fresh DB) is a no-op.
    conn.execute(
        text(
            "UPDATE credit_transactions SET balance_after = ("
            "  SELECT COALESCE(SUM(t2.amount), 0) FROM credit_transactions t2 "
            "  WHERE t2.user_id = credit_transactions.user_id "
            "    AND t2.id <= credit_transactions.id"
            ") WHERE balance_after IS NULL"
        )
    )
    # Match fresh installs' NOT NULL. Postgres can tighten in place after the
    # backfill; SQLite cannot without a table rebuild, so it stays nullable and
    # relies on the app layer plus the reconcile NULL check to keep it populated.
    if dialect == "postgresql":
        conn.execute(
            text(
                "ALTER TABLE credit_transactions "
                "ALTER COLUMN balance_after SET NOT NULL"
            )
        )


def _add_payment_intent_id(conn) -> None:
    if not _has_column(conn, "credit_transactions", "payment_intent_id"):
        conn.execute(
            text("ALTER TABLE credit_transactions ADD COLUMN payment_intent_id VARCHAR")
        )
    # matches the index create_all builds for the indexed model column
    conn.execute(
        text(
            "CREATE INDEX IF NOT EXISTS ix_credit_transactions_payment_intent_id "
            "ON credit_transactions (payment_intent_id)"
        )
    )


def _add_idempotency_key(conn) -> None:
    if not _has_column(conn, "credit_transactions", "idempotency_key"):
        conn.execute(
            text("ALTER TABLE credit_transactions ADD COLUMN idempotency_key VARCHAR")
        )
    # Scoped per user so one caller can't reuse another's key. A unique index
    # after the fact is fine on both backends; NULLs don't collide, so existing
    # rows are unaffected.
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_tx_user_idempotency_key "
            "ON credit_transactions (user_id, idempotency_key)"
        )
    )


STEPS: list[tuple[int, str, Callable]] = [
    (1, "add balance_after running-balance to credit_transactions", _add_balance_after),
    (2, "add payment_intent_id to credit_transactions", _add_payment_intent_id),
    (3, "add idempotency_key to credit_transactions", _add_idempotency_key),
]

HEAD = STEPS[-1][0] if STEPS else 0


def legacy_event_keyed_purchases(engine: Engine) -> int:
    """Count purchase rows still keyed on a Stripe event id (the pre-0.2 scheme).

    Such rows can't be de-duplicated against post-upgrade settlement events
    (which carry a different event id and no link back), so a delayed payment
    mid-settlement across the upgrade could be credited twice.
    """
    with engine.connect() as conn:
        # Stripe event ids start with 'evt_'; checkout session ids with 'cs_'.
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM credit_transactions "
                "WHERE kind = 'purchase' AND external_id LIKE 'evt_%'"
            )
        ).scalar()
    return int(count or 0)


def run_pending(engine: Engine) -> list[str]:
    """Apply pending migrations in order; return human-readable descriptions.

    Refuses to run if the ledger does not already reconcile — never backfill a
    running balance on top of existing drift.
    """
    with engine.begin() as conn:
        drift = _ledger_drift(conn)
        if drift:
            raise RuntimeError(
                f"refusing to migrate: {len(drift)} balance(s) disagree with the "
                f"ledger; reconcile first (first few: {drift[:3]})"
            )
        version = _ensure_version_row(conn)
        applied: list[str] = []
        for target, description, step in STEPS:
            if target > version:
                step(conn)
                _set_version(conn, target)
                applied.append(f"v{target}: {description}")
        return applied
