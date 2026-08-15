"""Database operations: users, atomic credit movements, and ledger queries."""

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth, models


def create_user(
    db: Session,
    username: str,
    api_key_hash: str,
    key_last4: str = "",
    credits: int = 0,
    is_admin: bool = False,
) -> models.User:
    """Create a user, recording any initial credits as a grant ledger row."""
    user = models.User(
        username=username,
        api_key_hash=api_key_hash,
        key_last4=key_last4,
        credits=credits,
        is_admin=is_admin,
    )
    if credits < 0:
        raise ValueError("initial credits cannot be negative")
    db.add(user)
    if credits:
        db.add(
            models.CreditTransaction(
                user=user, amount=credits, kind="grant", balance_after=credits
            )
        )
    db.commit()
    db.refresh(user)
    return user


def get_user(db: Session, user_id: int) -> models.User | None:
    """Return the user with the given id, or None."""
    return db.get(models.User, user_id)


def get_user_by_username(db: Session, username: str) -> models.User | None:
    """Return the user with the given username, or None."""
    return db.query(models.User).filter(models.User.username == username).first()


def get_user_by_api_key(db: Session, api_key: str) -> models.User | None:
    """Return the user owning the given API key (matched by hash), or None."""
    hash_ = auth.get_api_key_hash(api_key)
    return db.query(models.User).filter(models.User.api_key_hash == hash_).first()


def charge_user(
    db: Session, user: models.User, cost: int, endpoint: str | None = None
) -> bool:
    """Atomically deduct `cost` if the balance suffices.

    The ledger row is written in the same transaction as the balance update.
    Returns False (and leaves the balance untouched) when credits are short.
    A `cost` of 0 is a no-op that succeeds without a ledger row; a negative
    `cost` raises ValueError, since a charge must never raise a balance.
    """
    if cost < 0:
        raise ValueError("charge cost cannot be negative")
    if cost == 0:
        return True
    updated = (
        db.query(models.User)
        .filter(models.User.id == user.id, models.User.credits >= cost)
        .update({models.User.credits: models.User.credits - cost})
    )
    if not updated:
        db.rollback()
        db.refresh(user)
        return False
    db.refresh(user)  # balance after the debit, our own in-transaction write
    db.add(
        models.CreditTransaction(
            user_id=user.id,
            amount=-cost,
            kind="charge",
            endpoint=endpoint,
            balance_after=user.credits,
        )
    )
    db.commit()
    db.refresh(user)
    return True


def refund_user(
    db: Session, user: models.User, amount: int, endpoint: str | None = None
) -> models.User:
    """Return `amount` credits to the user with a compensating ledger row.

    An `amount` of 0 is a no-op that writes no ledger row; a negative `amount`
    raises ValueError, since a refund must never deduct.
    """
    if amount < 0:
        raise ValueError("refund amount cannot be negative")
    if amount == 0:
        return user
    db.query(models.User).filter(models.User.id == user.id).update(
        {models.User.credits: models.User.credits + amount}
    )
    db.refresh(user)
    db.add(
        models.CreditTransaction(
            user_id=user.id,
            amount=amount,
            kind="refund",
            endpoint=endpoint,
            balance_after=user.credits,
        )
    )
    db.commit()
    db.refresh(user)
    return user


def grant_credits(
    db: Session,
    user: models.User,
    amount: int,
    kind: str = "grant",
    external_id: str | None = None,
    amount_cents: int | None = None,
) -> bool:
    """Atomically add credits with a ledger row.

    Returns False when `external_id` was already processed, making
    event-driven crediting (e.g. Stripe webhooks) idempotent. A negative
    `amount` raises ValueError, since a grant must never deduct.
    """
    if amount < 0:
        raise ValueError("grant amount cannot be negative")
    db.query(models.User).filter(models.User.id == user.id).update(
        {models.User.credits: models.User.credits + amount}
    )
    db.refresh(user)
    db.add(
        models.CreditTransaction(
            user_id=user.id,
            amount=amount,
            kind=kind,
            external_id=external_id,
            amount_cents=amount_cents,
            balance_after=user.credits,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Only a duplicate external_id means "already processed" (idempotent
        # replay). Any other integrity failure is a real error we must not
        # swallow, or a valid grant would vanish silently.
        if external_id is not None and external_id_exists(db, external_id):
            return False
        raise
    db.refresh(user)
    return True


def external_id_exists(db: Session, external_id: str) -> bool:
    """Whether a ledger row already carries this external id."""
    return (
        db.query(models.CreditTransaction.id)
        .filter(models.CreditTransaction.external_id == external_id)
        .first()
        is not None
    )


def list_transactions(
    db: Session,
    user_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[models.CreditTransaction]:
    """Return ledger rows, newest first, optionally for a single user."""
    query = db.query(models.CreditTransaction)
    if user_id is not None:
        query = query.filter(models.CreditTransaction.user_id == user_id)
    return (
        query.order_by(models.CreditTransaction.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def list_users_with_stats(db: Session) -> list[dict]:
    """Return, per user, balance plus consumption and last activity from the ledger."""
    consumed = func.sum(
        case(
            (
                models.CreditTransaction.kind == "charge",
                -models.CreditTransaction.amount,
            ),
            else_=0,
        )
    )
    rows = (
        db.query(
            models.User,
            consumed.label("consumed"),
            func.max(models.CreditTransaction.created_at).label("last_activity"),
        )
        .outerjoin(
            models.CreditTransaction, models.CreditTransaction.user_id == models.User.id
        )
        .group_by(models.User.id)
        .order_by(models.User.id)
        .all()
    )
    return [
        {
            "id": user.id,
            "username": user.username,
            "key_last4": user.key_last4,
            "balance": user.credits,
            "is_admin": user.is_admin,
            "consumed": int(consumed_ or 0),
            "last_activity": last_activity.isoformat() if last_activity else None,
        }
        for user, consumed_, last_activity in rows
    ]


def aggregate_stats(db: Session) -> dict:
    """Return system totals: users, credits outstanding/consumed/purchased, revenue."""
    user_count = db.query(func.count(models.User.id)).scalar() or 0
    outstanding = db.query(func.sum(models.User.credits)).scalar() or 0

    def _sum_for(kind: str, column) -> int:
        value = (
            db.query(func.sum(column))
            .filter(models.CreditTransaction.kind == kind)
            .scalar()
        )
        return int(value or 0)

    return {
        "users": int(user_count),
        "credits_outstanding": int(outstanding),
        "credits_consumed": -_sum_for("charge", models.CreditTransaction.amount),
        "credits_purchased": _sum_for("purchase", models.CreditTransaction.amount),
        "revenue_cents": _sum_for("purchase", models.CreditTransaction.amount_cents),
    }


def find_balance_discrepancies(db: Session) -> list[dict]:
    """Return users whose ledger fails the running-balance consistency check.

    For each user, walking the append-only ledger oldest-first, every row's
    `balance_after` must equal the cumulative `SUM(amount)` up to that row, and
    the final running total must equal the cached `credits`. This validates the
    whole chain, not just the latest row, so corruption or a NULL in any earlier
    row is caught. An empty list means every balance reconciles.
    """
    users = db.query(models.User).order_by(models.User.id).all()
    discrepancies = []
    for user in users:
        rows = (
            db.query(models.CreditTransaction)
            .filter(models.CreditTransaction.user_id == user.id)
            .order_by(models.CreditTransaction.id)
            .all()
        )
        running = 0
        bad_rows = 0
        null_rows = 0
        for row in rows:
            running += row.amount
            if row.balance_after is None:
                null_rows += 1
            elif row.balance_after != running:
                bad_rows += 1
        if user.credits != running or bad_rows or null_rows:
            discrepancies.append(
                {
                    "id": user.id,
                    "username": user.username,
                    "cached": user.credits,
                    "ledger": running,
                    "balance_after": rows[-1].balance_after if rows else 0,
                    "bad_balance_after_rows": bad_rows,
                    "null_balance_after_rows": null_rows,
                }
            )
    return discrepancies


def set_admin(db: Session, user: models.User, is_admin: bool) -> models.User:
    """Grant or revoke the user's admin flag."""
    user.is_admin = is_admin
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
