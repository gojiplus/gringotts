"""FastAPI dependencies: authentication, admin gating, and the charge() core."""

import asyncio
import logging
from collections.abc import Callable, Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import crud, db
from .db import get_session
from .exceptions import InvalidAPIKeyError, PaymentRequiredError
from .models import User

logger = logging.getLogger(__name__)

CreditedUser = User

CostSpec = int | Callable[[Request], int]

API_KEY_HEADER = "X-API-Key"
IDEMPOTENCY_HEADER = "Idempotency-Key"


def authenticate(db: Session, api_key: str | None) -> User:
    """Resolve an API key to its user or raise a 401."""
    if not api_key:
        raise InvalidAPIKeyError()
    user = crud.get_user_by_api_key(db, api_key)
    if user is None:
        raise InvalidAPIKeyError()
    return user


def require_admin(request: Request, db: Session = Depends(get_session)) -> User:
    """FastAPI dependency: authenticate and require the admin flag (403 otherwise)."""
    user = authenticate(db, request.headers.get(API_KEY_HEADER))
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def charge(cost: CostSpec) -> Callable[..., Iterator[User]]:
    """Build the dependency that authenticates and charges for a request.

    The dependency yields the charged user; if the endpoint raises, the
    charge is refunded with a compensating ledger entry.

    Args:
        cost: Credits to charge — an int, or a callable computing it from
            the request (e.g. per-unit pricing).

    Returns:
        A FastAPI dependency usable as ``Depends(charge(5))``.

    Example:
        >>> from fastapi import Depends
        >>> from gringotts import CreditedUser, charge
        >>> @app.post("/predict")  # doctest: +SKIP
        ... def predict(user: CreditedUser = Depends(charge(1))):
        ...     return {"credits_left": user.credits}
    """

    def dependency(request: Request) -> Iterator[User]:
        # Credit movement runs on its own session, never the host app's
        # request session — so gringotts never commits or rolls back the
        # caller's pending work.
        session = db.SessionLocal()
        try:
            user = authenticate(session, request.headers.get(API_KEY_HEADER))
            amount = cost(request) if callable(cost) else cost
            if amount < 0:
                raise HTTPException(status_code=400, detail="Invalid credit cost")
            endpoint = request.url.path
            # An optional Idempotency-Key makes a retried request charge once.
            key = request.headers.get(IDEMPOTENCY_HEADER)
            result = crud.charge_user(
                session, user, amount, endpoint=endpoint, idempotency_key=key
            )
            if result is crud.ChargeResult.INSUFFICIENT:
                raise PaymentRequiredError(cost=amount, balance=user.credits)
            if result is crud.ChargeResult.CONFLICT:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency-Key reused with a different request",
                )
            # Only refund a debit this invocation actually made — a replayed key
            # made no debit, so refunding it would mint credits.
            debited = result is crud.ChargeResult.CHARGED
            user_id = user.id
            try:
                yield user
            except (Exception, asyncio.CancelledError):
                # Refund on any abnormal exit, including a client disconnect
                # (CancelledError is a BaseException, so `except Exception`
                # alone would let it consume credits for undelivered work).
                # GeneratorExit is deliberately not caught: it fires on normal
                # close and must not trigger a refund.
                if debited:
                    _refund_on_fresh_session(user_id, amount, endpoint, key)
                raise
        finally:
            session.close()

    return dependency


def _refund_on_fresh_session(
    user_id: int, amount: int, endpoint: str, idempotency_key: str | None = None
) -> None:
    """Compensate a charge on a brand-new session.

    Using a fresh session means a failing handler's uncommitted mutations to
    the yielded user (on the charge session) are discarded when that session
    closes, never committed by the refund. When the charge carried an
    idempotency key, the key is released so a retry charges fresh rather than
    replaying a debit that was refunded.
    """
    session = db.SessionLocal()
    try:
        user = crud.get_user(session, user_id)
        if user is not None:
            crud.refund_user(session, user, amount, endpoint=endpoint)
            if idempotency_key is not None:
                crud.release_idempotency_key(session, user_id, idempotency_key)
    except Exception:
        # Best-effort: never let a failed refund mask the original exception.
        logger.exception("gringotts: refund failed for user %s", user_id)
    finally:
        session.close()
