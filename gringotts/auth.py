import hashlib
import secrets
from sqlalchemy.orm import Session


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def get_api_key_hash(api_key: str) -> str:
    """Return a deterministic hash for the given API key."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def verify_api_key(api_key: str, hashed: str) -> bool:
    return get_api_key_hash(api_key) == hashed


def create_user_with_key(db: Session, username: str, credits: int = 0):
    from . import crud  # lazy import to avoid circular

    api_key = generate_api_key()
    hash_ = get_api_key_hash(api_key)
    user = crud.create_user(db, username=username, api_key_hash=hash_, credits=credits)
    return user, api_key
