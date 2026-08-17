"""Database operations: users, atomic credit movements, and ledger queries."""

import logging
import secrets

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth, models

logger = logging.getLogger(__name__)


def _require_integer_amount(amount: int) -> None:
    """Reject values that cannot belong in the integer credit ledger."""
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise TypeError("amount must be an integer")


def _normalize_currency(currency: str | None) -> str | None:
    """Validate and normalize a three-letter ISO currency code."""
    if currency is None:
        return None
    if not isinstance(currency, str):
        raise TypeError("currency must be a string")
    if len(currency) != 3 or not currency.isalpha():
        raise ValueError("currency must be a three-letter ISO code")
    return currency.lower()


def create_user(
    db: Session,
    username: str,
    api_key_hash: str,
    key_last4: str = "",
    credits: int = 0,
    is_admin: bool = False,
) -> models.User:
    """Create a user, recording any initial credits as a grant ledger row."""
    _require_integer_amount(credits)
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
    db: Session,
    user: models.User,
    cost: int,
    endpoint: str | None = None,
) -> bool:
    """Atomically deduct `cost` if the balance suffices; return whether it did.

    The ledger row is written in the same transaction as the balance update, via
    a compare-and-set `UPDATE ... WHERE credits >= cost` so two concurrent charges
    can't both pass a stale balance check. A `cost` of 0 is a no-op that returns
    True and writes no row; a negative `cost` raises ValueError, since a charge
    must never raise a balance. Safe retries are handled one layer up by
    :class:`~gringotts.idempotency.IdempotencyMiddleware`, not here.
    """
    _require_integer_amount(cost)
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
    _require_integer_amount(amount)
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
    payment_intent_id: str | None = None,
    currency: str | None = None,
) -> bool:
    """Atomically add credits with a ledger row.

    Returns False when `external_id` was already processed, making event-driven
    crediting (Stripe webhooks) idempotent. A negative `amount` raises ValueError,
    since a grant must never deduct. HTTP-level safe-retry for the admin grant
    route is handled by :class:`~gringotts.idempotency.IdempotencyMiddleware`.
    """
    _require_integer_amount(amount)
    if amount < 0:
        raise ValueError("grant amount cannot be negative")
    normalized_currency = _normalize_currency(currency)
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
            payment_intent_id=payment_intent_id,
            currency=normalized_currency,
            balance_after=user.credits,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # A duplicate external_id means "already processed" (idempotent replay).
        # Any other integrity failure is a real error we must not swallow, or a
        # valid grant would vanish silently.
        if external_id is not None and external_id_exists(db, external_id):
            return False
        raise
    db.refresh(user)
    return True


def create_checkout_order(
    db: Session,
    user: models.User,
    credits: int,
    amount_cents: int,
    currency: str,
) -> models.CheckoutOrder:
    """Persist the exact entitlement authorized before creating Stripe Checkout."""
    _require_integer_amount(credits)
    _require_integer_amount(amount_cents)
    if credits <= 0 or amount_cents < 0:
        raise ValueError("invalid Checkout order amounts")
    normalized_currency = _normalize_currency(currency)
    if normalized_currency is None:
        raise ValueError("Checkout order currency is required")
    order = models.CheckoutOrder(
        id=secrets.token_hex(16),
        user_id=user.id,
        credits=credits,
        amount_cents=amount_cents,
        currency=normalized_currency,
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


def bind_checkout_order(
    db: Session,
    order_id: str,
    stripe_session_id: str,
    *,
    commit: bool,
) -> models.CheckoutOrder | None:
    """Atomically bind an order once to its Stripe Checkout Session."""
    updated = (
        db.query(models.CheckoutOrder)
        .filter(
            models.CheckoutOrder.id == order_id,
            (models.CheckoutOrder.stripe_session_id.is_(None))
            | (models.CheckoutOrder.stripe_session_id == stripe_session_id),
        )
        .update(
            {models.CheckoutOrder.stripe_session_id: stripe_session_id},
            synchronize_session=False,
        )
    )
    if not updated:
        existing = db.get(models.CheckoutOrder, order_id)
        if existing is None:
            return None
        raise ValueError("Checkout order is already bound to another Session")
    order = db.get(models.CheckoutOrder, order_id)
    assert order is not None  # noqa: S101 - the successful UPDATE proves it exists
    db.refresh(order)
    if commit:
        db.commit()
        db.refresh(order)
    return order


def external_id_exists(db: Session, external_id: str) -> bool:
    """Whether a ledger row already carries this external id."""
    return (
        db.query(models.CreditTransaction.id)
        .filter(models.CreditTransaction.external_id == external_id)
        .first()
        is not None
    )


def find_purchase_by_payment_intent(
    db: Session, payment_intent_id: str | None
) -> models.CreditTransaction | None:
    """Return the earliest purchase row for a Stripe PaymentIntent, or None.

    A falsy `payment_intent_id` matches nothing — never a NULL-keyed pre-0.3
    purchase — so a reversal with no PaymentIntent can't hit an unrelated user.
    """
    if not payment_intent_id:
        return None
    return (
        db.query(models.CreditTransaction)
        .filter(
            models.CreditTransaction.kind == "purchase",
            models.CreditTransaction.payment_intent_id == payment_intent_id,
        )
        .order_by(models.CreditTransaction.id)
        .first()
    )


def lock_user(db: Session, user_id: int) -> models.User | None:
    """Take a write lock on the user row that serializes reversal math per user.

    `SELECT ... FOR UPDATE` is a no-op on SQLite, so instead we issue a real
    no-op `UPDATE` of the row: that acquires the row lock on Postgres and the
    database write lock on SQLite, both held until commit. So two concurrent
    reversals for the same purchase can't both read stale cumulative totals.
    Returns None if the user does not exist.
    """
    updated = (
        db.query(models.User)
        .filter(models.User.id == user_id)
        .update({models.User.credits: models.User.credits})
    )
    if not updated:
        return None
    return db.get(models.User, user_id)


def clawback_totals(db: Session, payment_intent_id: str) -> tuple[int, int]:
    """Cumulative (reversed cents, credits clawed) for a purchase's clawbacks.

    Lets refund/dispute handling compute the target total clawback and deduct
    only the delta, so independent per-event rounding can't over- or under-claw.
    """
    # Include reinstatements: a won dispute writes a reinstate row carrying the
    # negative of the withdrawn cents, so its contribution cancels here and a
    # later refund on the same purchase is measured against the true net.
    row = (
        db.query(
            func.coalesce(func.sum(models.CreditTransaction.amount_cents), 0),
            func.coalesce(func.sum(-models.CreditTransaction.amount), 0),
        )
        .filter(
            models.CreditTransaction.kind.in_(("clawback", "reinstate")),
            models.CreditTransaction.payment_intent_id == payment_intent_id,
        )
        .one()
    )
    return int(row[0]), int(row[1])


def clawback_credits(
    db: Session,
    user: models.User,
    amount: int,
    *,
    external_id: str,
    kind: str = "clawback",
    endpoint: str | None = None,
    amount_cents: int | None = None,
    payment_intent_id: str | None = None,
    currency: str | None = None,
) -> int:
    """Deduct up to `amount` credits (clamped at zero) with a ledger row.

    Used to reverse a refunded or disputed purchase. Never drives the balance
    negative — it deducts only what the user still holds. Returns the amount
    actually deducted. Idempotent on `external_id`: a redelivered event finds the
    existing row and deducts nothing. A negative `amount` raises ValueError.
    `amount_cents` records the reversed money and `payment_intent_id` ties the row
    to its purchase, so cumulative clawback math stays exact across partials.
    """
    _require_integer_amount(amount)
    if amount < 0:
        raise ValueError("clawback amount cannot be negative")
    normalized_currency = _normalize_currency(currency)
    # Lock the user row so the clamp reads a stable balance (Postgres); SQLite
    # serializes writers, so the read-decide-write is atomic there too.
    current = (
        db.query(models.User.credits)
        .filter(models.User.id == user.id)
        .with_for_update()
        .scalar()
    )
    deducted = min(amount, current) if current is not None else 0
    if deducted:
        db.query(models.User).filter(models.User.id == user.id).update(
            {models.User.credits: models.User.credits - deducted}
        )
    db.refresh(user)
    db.add(
        models.CreditTransaction(
            user_id=user.id,
            amount=-deducted,
            kind=kind,
            external_id=external_id,
            endpoint=endpoint,
            amount_cents=amount_cents,
            payment_intent_id=payment_intent_id,
            currency=normalized_currency,
            balance_after=user.credits,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if external_id_exists(db, external_id):
            return 0  # already processed (idempotent replay)
        raise
    db.refresh(user)
    if deducted < amount:
        logger.warning(
            "gringotts clawback clamped: wanted %s, deducted %s for user %s "
            "(external_id=%s)",
            amount,
            deducted,
            user.id,
            external_id,
        )
    return deducted


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
    """Return, per user, balance plus consumption and last activity from the ledger.

    `consumed` is net of refunds: a charge adds to it, a refund (which reverses a
    charge) subtracts, so a fully refunded request counts as zero consumption.
    """
    consumed = func.sum(
        case(
            (
                models.CreditTransaction.kind == "charge",
                -models.CreditTransaction.amount,
            ),
            (
                models.CreditTransaction.kind == "refund",
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
    """Return system totals, keeping revenue separated by ISO currency."""
    user_count = db.query(func.count(models.User.id)).scalar() or 0
    outstanding = db.query(func.sum(models.User.credits)).scalar() or 0

    def _sum_for(kind: str, column) -> int:
        value = (
            db.query(func.sum(column))
            .filter(models.CreditTransaction.kind == kind)
            .scalar()
        )
        return int(value or 0)

    charged = -_sum_for("charge", models.CreditTransaction.amount)
    refunded = _sum_for("refund", models.CreditTransaction.amount)
    # clawback rows are negative, reinstate positive; adding both nets a
    # refunded/disputed purchase back out so the metric can't overstate.
    purchased = (
        _sum_for("purchase", models.CreditTransaction.amount)
        + _sum_for("clawback", models.CreditTransaction.amount)
        + _sum_for("reinstate", models.CreditTransaction.amount)
    )
    money_rows = (
        db.query(
            models.CreditTransaction.currency,
            models.CreditTransaction.kind,
            func.sum(models.CreditTransaction.amount_cents),
        )
        .filter(
            models.CreditTransaction.kind.in_(("purchase", "clawback", "reinstate")),
            models.CreditTransaction.amount_cents.is_not(None),
        )
        .group_by(models.CreditTransaction.currency, models.CreditTransaction.kind)
        .all()
    )
    revenue: dict[str, int] = {}
    for currency, kind, total in money_rows:
        key = currency or "unknown"
        # Purchases add money. Clawbacks carry positive reversed minor units and
        # reinstatements carry the negative offset, so both are subtracted.
        delta = int(total or 0) if kind == "purchase" else -int(total or 0)
        revenue[key] = revenue.get(key, 0) + delta
    return {
        "users": int(user_count),
        "credits_outstanding": int(outstanding),
        # net of refunds: a fully refunded charge is zero consumption
        "credits_consumed": charged - refunded,
        # net of clawbacks: a refunded/disputed purchase nets back out
        "credits_purchased": purchased,
        "revenue_by_currency": dict(sorted(revenue.items())),
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


def purge_idempotency_records(
    db: Session, older_than_seconds: float, include_in_flight: bool = False
) -> int:
    """Delete idempotency records older than `older_than_seconds`; return the count.

    Response caching stores a row per keyed request; this reclaims space from keys
    that are never retried (the middleware also expires records lazily on reuse).
    Only **completed** records are removed by default: an in-flight (`completed=
    False`) row is a live lock, and deleting it could free a key whose request is
    still running, letting a retry execute the side effect again. Pass
    `include_in_flight=True` only to clear locks left by crashed requests, when you
    know none are genuinely still running.
    """
    from datetime import UTC, datetime, timedelta

    cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
    query = db.query(models.IdempotencyRecord).filter(
        models.IdempotencyRecord.created_at < cutoff
    )
    if not include_in_flight:
        query = query.filter(models.IdempotencyRecord.completed.is_(True))
    deleted = query.delete(synchronize_session=False)
    db.commit()
    return int(deleted)


def set_admin(db: Session, user: models.User, is_admin: bool) -> models.User:
    """Grant or revoke the user's admin flag."""
    user.is_admin = is_admin
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
