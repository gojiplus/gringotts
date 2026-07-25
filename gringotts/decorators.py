"""Legacy decorator API. Prefer ``Depends(charge(cost))`` from gringotts.dependencies."""

import inspect
from functools import wraps
from typing import Any

from fastapi import Request

from . import crud, db
from .dependencies import API_KEY_HEADER, authenticate


def _extract_request(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Request | None:
    request = kwargs.get("request")
    if request is None:
        for arg in args:
            if isinstance(arg, Request):
                request = arg
                break
    return request


def requires_credits(cost: int = 1):
    """Charge `cost` credits before running the endpoint; refund if it raises.

    The endpoint must accept the ``Request`` object. New code should use
    ``Depends(charge(cost))`` instead, which also injects the user.
    """

    def decorator(func):
        def _charge(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[int, str]:
            request = _extract_request(args, kwargs)
            if request is None:
                raise RuntimeError("Request object not found")

            endpoint = request.url.path
            session = db.SessionLocal()
            try:
                user = authenticate(session, request.headers.get(API_KEY_HEADER))
                from .exceptions import PaymentRequiredError

                if not crud.charge_user(session, user, cost, endpoint=endpoint):
                    raise PaymentRequiredError(cost=cost, balance=user.credits)
                return user.id, endpoint
            finally:
                session.close()

        def _refund(user_id: int, endpoint: str) -> None:
            session = db.SessionLocal()
            try:
                user = crud.get_user(session, user_id)
                if user is not None:
                    crud.refund_user(session, user, cost, endpoint=endpoint)
            finally:
                session.close()

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                user_id, endpoint = _charge(args, kwargs)
                try:
                    return await func(*args, **kwargs)
                except Exception:
                    _refund(user_id, endpoint)
                    raise

            return async_wrapper

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            user_id, endpoint = _charge(args, kwargs)
            try:
                return func(*args, **kwargs)
            except Exception:
                _refund(user_id, endpoint)
                raise

        return sync_wrapper

    return decorator
