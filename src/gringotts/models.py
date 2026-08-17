"""SQLAlchemy models: users and the append-only credit ledger."""

from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class User(Base):
    """An API consumer: hashed key, current balance, and optional admin flag."""

    __tablename__ = "users"
    # Balances must never go negative; a database-level backstop catches any
    # code path that would violate the invariant the app layer also enforces.
    __table_args__ = (CheckConstraint("credits >= 0", name="ck_users_credits_nonneg"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    api_key_hash: Mapped[str] = mapped_column(String, unique=True)
    key_last4: Mapped[str] = mapped_column(String(4), default="")
    credits: Mapped[int] = mapped_column(default=0)
    is_admin: Mapped[bool] = mapped_column(default=False)

    transactions: Mapped[list["CreditTransaction"]] = relationship(
        back_populates="user"
    )


class CreditTransaction(Base):
    """Append-only ledger row.

    The sum of amounts per user always equals the user's balance, and
    `balance_after` records that running balance as of this row — so the
    balance is auditable per row and drift is structurally detectable.
    """

    __tablename__ = "credit_transactions"
    # balance never goes negative; the running balance is stored per row so
    # the ledger is self-verifying rather than only reconciled against a cache.
    __table_args__ = (
        CheckConstraint("balance_after >= 0", name="ck_credit_tx_balance_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # negative for charges, positive for grants/refunds/purchases
    amount: Mapped[int]
    # the user's balance immediately after this row was written
    balance_after: Mapped[int]
    kind: Mapped[str] = mapped_column(String(16))
    # unique id of the originating external event (e.g. Stripe event id);
    # the unique constraint is what makes webhook crediting idempotent
    external_id: Mapped[str | None] = mapped_column(String, unique=True, default=None)
    endpoint: Mapped[str | None] = mapped_column(String, default=None)
    # money actually paid, set only on purchase rows (Checkout amount_total)
    amount_cents: Mapped[int | None] = mapped_column(default=None)
    # ISO currency for every purchase/reversal money amount. Historical rows
    # migrated from older releases remain NULL and are reported as unknown.
    currency: Mapped[str | None] = mapped_column(String(3), default=None)
    # Stripe PaymentIntent id, set on purchase rows; lets refund/dispute events
    # (which carry payment_intent, not the checkout session) find this purchase
    payment_intent_id: Mapped[str | None] = mapped_column(
        String, index=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    user: Mapped[User] = relationship(back_populates="transactions")


class CheckoutOrder(Base):
    """Locally authorized Stripe Checkout order awaiting fulfillment.

    The webhook grants only an exact match to one of these rows. This binds a
    Stripe Session to the user, credits, amount, and currency approved by the
    application before redirecting the buyer to Stripe.
    """

    __tablename__ = "checkout_orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    stripe_session_id: Mapped[str | None] = mapped_column(
        String, unique=True, index=True, default=None
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    credits: Mapped[int]
    amount_cents: Mapped[int]
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class IdempotencyRecord(Base):
    """A stored HTTP response, keyed by caller and Idempotency-Key.

    An ``Idempotency-Key`` header makes a mutating request safe to retry: the
    first request runs and its response is captured here; any later request with
    the same key from the same caller returns the stored response (or a marker for
    a non-cacheable body) without re-running the handler — so a retried charge or
    grant applies exactly once. Scoped by ``api_key_hash`` (the caller), so one
    caller can never replay another's key. ``request_fingerprint`` guards against
    reusing a key for a materially different request (method, path, headers, or body),
    which returns a 409.
    """

    __tablename__ = "idempotency_records"
    __table_args__ = (
        # one row per (caller, key); the unique index is what serializes a
        # concurrent first-and-retry race down to a single owner
        Index(
            "uq_idempotency_caller_key",
            "api_key_hash",
            "idempotency_key",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # SHA-256 of the caller's API key — identifies the caller without storing the
    # key, and scopes idempotency keys per caller
    api_key_hash: Mapped[str] = mapped_column(String, index=True)
    idempotency_key: Mapped[str] = mapped_column(String)
    # SHA-256 of method + path + query + headers + body; a mismatch is a 409
    request_fingerprint: Mapped[str] = mapped_column(String)
    # False while the first request is in flight; True once its response is stored
    completed: Mapped[bool] = mapped_column(default=False)
    status_code: Mapped[int | None] = mapped_column(default=None)
    response_body: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    # the captured response headers, JSON list of [name, value] latin-1 strings,
    # so an ordinary replay reproduces Location / Set-Cookie / HX-Trigger, etc.
    response_headers: Mapped[str | None] = mapped_column(String, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
