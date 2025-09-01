import inspect
from functools import wraps

from fastapi import HTTPException, Request

from .db import SessionLocal
from . import crud


class InvalidAPIKey(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=401, detail="Invalid API key")


class InsufficientCredits(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=402, detail="Insufficient credits")


def _extract_request(args, kwargs):
    request = kwargs.get("request")
    if request is None:
        for arg in args:
            if isinstance(arg, Request):
                request = arg
                break
    return request


def requires_credits(cost: int = 1):
    """Decorator to enforce that a request has enough credits."""

    def decorator(func):
        if inspect.iscoroutinefunction(func):
            async def async_wrapper(*args, **kwargs):
                request = _extract_request(args, kwargs)
                if request is None:
                    raise RuntimeError("Request object not found")

                api_key = request.headers.get("X-API-Key")
                if not api_key:
                    raise InvalidAPIKey()

                db = SessionLocal()
                try:
                    user = crud.get_user_by_api_key(db, api_key)
                    if not user:
                        raise InvalidAPIKey()
                    if user.credits < cost:
                        raise InsufficientCredits()
                    crud.update_user_credits(db, user, -cost)
                    crud.log_api_call(db, user, request.url.path, cost)
                finally:
                    db.close()
                return await func(*args, **kwargs)

            return wraps(func)(async_wrapper)
        else:
            def sync_wrapper(*args, **kwargs):
                request = _extract_request(args, kwargs)
                if request is None:
                    raise RuntimeError("Request object not found")

                api_key = request.headers.get("X-API-Key")
                if not api_key:
                    raise InvalidAPIKey()

                db = SessionLocal()
                try:
                    user = crud.get_user_by_api_key(db, api_key)
                    if not user:
                        raise InvalidAPIKey()
                    if user.credits < cost:
                        raise InsufficientCredits()
                    crud.update_user_credits(db, user, -cost)
                    crud.log_api_call(db, user, getattr(request, "path", ""), cost)
                finally:
                    db.close()
                return func(*args, **kwargs)

            return wraps(func)(sync_wrapper)

    return decorator
