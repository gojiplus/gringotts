from sqlalchemy.orm import Session

from . import auth, models


def create_user(db: Session, username: str, api_key_hash: str, credits: int = 0) -> models.User:
    user = models.User(username=username, api_key_hash=api_key_hash, credits=credits)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_user_by_api_key(db: Session, api_key: str) -> models.User | None:
    """Return the user matching the given API key."""
    hash_ = auth.get_api_key_hash(api_key)
    return db.query(models.User).filter(models.User.api_key_hash == hash_).first()


def update_user_credits(db: Session, user: models.User, delta: int) -> models.User:
    user.credits += delta
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def deduct_user_credits(db: Session, user: models.User, cost: int) -> bool:
    """Atomically deduct credits if the user has enough.

    Returns True if deduction succeeded, False otherwise.
    """
    updated = (
        db.query(models.User)
        .filter(models.User.id == user.id, models.User.credits >= cost)
        .update({models.User.credits: models.User.credits - cost})
    )
    if not updated:
        db.rollback()
        return False
    db.commit()
    db.refresh(user)
    return True


def log_api_call(db: Session, user: models.User, endpoint: str, cost: int) -> models.APICall:
    call = models.APICall(user_id=user.id, endpoint=endpoint, cost=cost)
    db.add(call)
    db.commit()
    db.refresh(call)
    return call
