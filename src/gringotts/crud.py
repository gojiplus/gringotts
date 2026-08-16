"""Database operations: users, atomic credit movements, and ledger queries."""

import enum
import logging

from sqlalchemy import case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import auth, models

logger = logging.getLogger(__name__)


class ChargeResult(enum.Enum):
    """Outcome of :func:`charge_user`.

    ``CHARGED`` means this call debited the balance (and must be refunded if the
    request then fails); ``REPLAYED`` means an idempotent retry matched a prior
    charge and nothing was debited (so it must *not* be refunded); ``INSUFFICIENT``
    means too few credits; ``CONFLICT`` means the ``idempotency_key`` was reused
    for a different cost or endpoint.
    """

    CHARGED = "charged"
    REPLAYED = "replayed"
    INSUFFICIENT = "insufficient"
    CONFLICT = "conflict"


def keyed_row(
    db: Session, user_id: int, idempotency_key: str
) -> models.CreditTransaction | None:
    """Return this user's ledger row for an idempotency key, or None.

    Scoped to ``user_id`` so one caller can never see another's keyed row.
    """
    return (
        db.query(models.CreditTransaction)
        .filter(
            models.CreditTransaction.user_id == user_id,
            models.CreditTransaction.idempotency_key == idempotency_key,
        )
        .first()
    )


def release_idempotency_key(db: Session, user_id: int, idempotency_key: str) -> None:
    """Clear a user's idempotency key from its ledger row so it can be reused.

    Called when a keyed charge is refunded (its handler failed): the debit was
    compensated, so a retry with the same key must charge fresh rather than
    replay — otherwise a later successful retry would deliver paid work for free.
    Only the operational key is cleared; the ledger amounts stay immutable.
    """
    db.query(models.CreditTransaction).filter(
        models.CreditTransaction.user_id == user_id,
        models.CreditTransaction.idempotency_key == idempotency_key,
    ).update({models.CreditTransaction.idempotency_key: None})
    db.commit()


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
    db: Session,
    user: models.User,
    cost: int,
    endpoint: str | None = None,
    idempotency_key: str | None = None,
) -> ChargeResult:
    """Atomically deduct `cost` if the balance suffices, returning a `ChargeResult`.

    The ledger row is written in the same transaction as the balance update. A
    `cost` of 0 is a no-op reported as ``CHARGED``; a negative `cost` raises
    ValueError, since a charge must never raise a balance. When `idempotency_key`
    is given (scoped to this user), a retry with the same key charges only once:
    a matching retry returns ``REPLAYED`` without deducting, and reusing the key
    for a different cost or endpoint returns ``CONFLICT``.
    """
    if cost < 0:
        raise ValueError("charge cost cannot be negative")
    if cost == 0:
        return ChargeResult.CHARGED

    def _classify_prior() -> ChargeResult | None:
        # REPLAYED if a matching prior charge exists, CONFLICT if it exists with
        # a different cost/endpoint, None if no prior row for this key. Applied at
        # the precheck AND in both race handlers, so a concurrent same-key retry
        # for a different operation can't slip past as a plain replay.
        if idempotency_key is None:
            return None
        prior = keyed_row(db, user.id, idempotency_key)
        if prior is None:
            return None
        if prior.amount != -cost or prior.endpoint != endpoint:
            return ChargeResult.CONFLICT
        return ChargeResult.REPLAYED

    precheck = _classify_prior()
    if precheck is not None:
        return precheck
    updated = (
        db.query(models.User)
        .filter(models.User.id == user.id, models.User.credits >= cost)
        .update({models.User.credits: models.User.credits - cost})
    )
    if not updated:
        db.rollback()
        db.refresh(user)
        # A concurrent same-key winner may have charged after our precheck; if so
        # this is a replay (or a conflict), not a genuine 402.
        raced = _classify_prior()
        return raced if raced is not None else ChargeResult.INSUFFICIENT
    db.refresh(user)  # balance after the debit, our own in-transaction write
    db.add(
        models.CreditTransaction(
            user_id=user.id,
            amount=-cost,
            kind="charge",
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            balance_after=user.credits,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # a concurrent retry with the same key won the race and charged already
        db.rollback()
        db.refresh(user)
        raced = _classify_prior()
        if raced is not None:
            return raced
        raise
    db.refresh(user)
    return ChargeResult.CHARGED


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
    payment_intent_id: str | None = None,
    idempotency_key: str | None = None,
) -> bool:
    """Atomically add credits with a ledger row.

    Returns False when `external_id` or `idempotency_key` was already processed,
    making event-driven or retried crediting idempotent. A negative `amount`
    raises ValueError, since a grant must never deduct.
    """
    if amount < 0:
        raise ValueError("grant amount cannot be negative")
    if idempotency_key is not None and keyed_row(db, user.id, idempotency_key):
        return False  # already granted under this key
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
            idempotency_key=idempotency_key,
            balance_after=user.credits,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # A duplicate external_id or idempotency_key means "already processed"
        # (idempotent replay). Any other integrity failure is a real error we
        # must not swallow, or a valid grant would vanish silently.
        if external_id is not None and external_id_exists(db, external_id):
            return False
        if idempotency_key is not None and keyed_row(db, user.id, idempotency_key):
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


def clawback_deducted(db: Session, external_id: str) -> int:
    """The credits a prior clawback row actually deducted (absolute value)."""
    row = (
        db.query(models.CreditTransaction.amount)
        .filter(models.CreditTransaction.external_id == external_id)
        .first()
    )
    return -int(row[0]) if row is not None else 0


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
) -> int:
    """Deduct up to `amount` credits (clamped at zero) with a ledger row.

    Used to reverse a refunded or disputed purchase. Never drives the balance
    negative — it deducts only what the user still holds. Returns the amount
    actually deducted. Idempotent on `external_id`: a redelivered event finds the
    existing row and deducts nothing. A negative `amount` raises ValueError.
    `amount_cents` records the reversed money and `payment_intent_id` ties the row
    to its purchase, so cumulative clawback math stays exact across partials.
    """
    if amount < 0:
        raise ValueError("clawback amount cannot be negative")
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

    charged = -_sum_for("charge", models.CreditTransaction.amount)
    refunded = _sum_for("refund", models.CreditTransaction.amount)
    # clawback rows are negative, reinstate positive; adding both nets a
    # refunded/disputed purchase back out so the metric can't overstate.
    purchased = (
        _sum_for("purchase", models.CreditTransaction.amount)
        + _sum_for("clawback", models.CreditTransaction.amount)
        + _sum_for("reinstate", models.CreditTransaction.amount)
    )
    # revenue = money paid minus money reversed. clawback rows carry the reversed
    # cents (positive), reinstate rows the negative offset, so subtracting both
    # nets a refunded/disputed payment out of revenue.
    reversed_cents = _sum_for(
        "clawback", models.CreditTransaction.amount_cents
    ) + _sum_for("reinstate", models.CreditTransaction.amount_cents)
    revenue_cents = (
        _sum_for("purchase", models.CreditTransaction.amount_cents) - reversed_cents
    )
    return {
        "users": int(user_count),
        "credits_outstanding": int(outstanding),
        # net of refunds: a fully refunded charge is zero consumption
        "credits_consumed": charged - refunded,
        # net of clawbacks: a refunded/disputed purchase nets back out
        "credits_purchased": purchased,
        "revenue_cents": revenue_cents,
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
