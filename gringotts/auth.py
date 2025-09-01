import secrets
from passlib.context import CryptContext
from sqlalchemy.orm import Session

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


def get_api_key_hash(api_key: str) -> str:
    return pwd_context.hash(api_key)


def verify_api_key(api_key: str, hashed: str) -> bool:
    return pwd_context.verify(api_key, hashed)


def create_user_with_key(db: Session, username: str, credits: int = 0):
    from . import crud  # lazy import to avoid circular

    api_key = generate_api_key()
    hash_ = get_api_key_hash(api_key)
    user = crud.create_user(db, username=username, api_key_hash=hash_, credits=credits)
    return user, api_key
