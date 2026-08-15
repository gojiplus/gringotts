"""SQLAlchemy models: users and the append-only credit ledger."""

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
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
    # Stripe PaymentIntent id, set on purchase rows; lets refund/dispute events
    # (which carry payment_intent, not the checkout session) find this purchase
    payment_intent_id: Mapped[str | None] = mapped_column(
        String, index=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    user: Mapped[User] = relationship(back_populates="transactions")
