"""SQLAlchemy models: users and the append-only credit ledger."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class User(Base):
    """An API consumer: hashed key, current balance, and optional admin flag."""

    __tablename__ = "users"

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

    The sum of amounts per user always equals the user's balance.
    """

    __tablename__ = "credit_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # negative for charges, positive for grants/refunds/purchases
    amount: Mapped[int]
    kind: Mapped[str] = mapped_column(String(16))
    # unique id of the originating external event (e.g. Stripe event id);
    # the unique constraint is what makes webhook crediting idempotent
    external_id: Mapped[str | None] = mapped_column(String, unique=True, default=None)
    endpoint: Mapped[str | None] = mapped_column(String, default=None)
    # money actually paid, set only on purchase rows (Checkout amount_total)
    amount_cents: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )

    user: Mapped[User] = relationship(back_populates="transactions")
