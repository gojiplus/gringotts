"""Prepaid credits for your FastAPI, with your own Stripe account."""

from .app import init_app
from .config import CreditPack, GringottsConfig
from .crud import grant_credits as grant
from .db import Base, SessionLocal, engine, get_session
from .dependencies import CreditedUser, charge
from .exceptions import InvalidAPIKeyError, PaymentRequiredError

__version__ = "0.1.0"

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
