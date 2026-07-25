import hashlib
import hmac
import secrets

from sqlalchemy.orm import Session

KEY_PREFIX = "gk_"


def generate_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def get_api_key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def verify_api_key(api_key: str, hashed: str) -> bool:
    return hmac.compare_digest(get_api_key_hash(api_key), hashed)


def create_user_with_key(db: Session, username: str, credits: int = 0, is_admin: bool = False):
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
