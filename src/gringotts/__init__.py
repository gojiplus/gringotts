"""Prepaid credits for your FastAPI, with your own Stripe account."""

from importlib.metadata import PackageNotFoundError, version

from .app import init_app
from .config import CreditPack, GringottsConfig
from .crud import grant_credits as grant
from .db import Base, SessionLocal, engine, get_session
from .dependencies import CreditedUser, charge
from .exceptions import InvalidAPIKeyError, PaymentRequiredError

try:
    __version__ = version("gringotts-api")
except PackageNotFoundError:  # pragma: no cover - not installed
    __version__ = "0.0.0"

__all__ = [
    "Base",
    "CreditPack",
    "CreditedUser",
    "GringottsConfig",
    "InvalidAPIKeyError",
    "PaymentRequiredError",
    "SessionLocal",
    "charge",
    "engine",
    "get_session",
    "grant",
    "init_app",
]
