"""API key generation, hashing, and user+key creation.

Keys are prefixed `gk_`, stored only as SHA-256 hashes, and shown exactly once.
"""

import hashlib
import hmac
import secrets

from sqlalchemy.orm import Session

KEY_PREFIX = "gk_"


def generate_api_key() -> str:
    """Return a new random API key with the `gk_` prefix."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def get_api_key_hash(api_key: str) -> str:
    """Return the SHA-256 hex digest under which a key is stored."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def verify_api_key(api_key: str, hashed: str) -> bool:
    """Check a key against its stored hash in constant time."""
    return hmac.compare_digest(get_api_key_hash(api_key), hashed)


def create_user_with_key(
    db: Session, username: str, credits: int = 0, is_admin: bool = False
):
    """Create a user and return `(user, api_key)` — the key's only appearance."""
    from . import crud

    api_key = generate_api_key()
    user = crud.create_user(
        db,
        username=username,
        api_key_hash=get_api_key_hash(api_key),
        key_last4=api_key[-4:],
        credits=credits,
        is_admin=is_admin,
    )
    return user, api_key
