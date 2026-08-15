"""FastAPI dependencies: authentication, admin gating, and the charge() core."""

from collections.abc import Callable, Iterator

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from . import crud, db
from .db import get_session
from .exceptions import InvalidAPIKeyError, PaymentRequiredError
from .models import User

CreditedUser = User

CostSpec = int | Callable[[Request], int]

API_KEY_HEADER = "X-API-Key"


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
            if not crud.charge_user(session, user, amount, endpoint=endpoint):
                raise PaymentRequiredError(cost=amount, balance=user.credits)
            user_id = user.id
            try:
                yield user
            except Exception:
                _refund_on_fresh_session(user_id, amount, endpoint)
                raise
        finally:
            session.close()

    return dependency


def _refund_on_fresh_session(user_id: int, amount: int, endpoint: str) -> None:
    """Compensate a charge on a brand-new session.

    Using a fresh session means a failing handler's uncommitted mutations to
    the yielded user (on the charge session) are discarded when that session
    closes, never committed by the refund.
    """
    session = db.SessionLocal()
    try:
        user = crud.get_user(session, user_id)
        if user is not None:
            crud.refund_user(session, user, amount, endpoint=endpoint)
    finally:
        session.close()
