from sqlalchemy.orm import Session

from . import models, auth


def create_user(db: Session, username: str, api_key_hash: str, credits: int = 0) -> models.User:
    user = models.User(username=username, api_key_hash=api_key_hash, credits=credits)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_user_by_api_key(db: Session, api_key: str) -> models.User | None:
    for user in db.query(models.User).all():
        if auth.verify_api_key(api_key, user.api_key_hash):
            return user
    return None


def update_user_credits(db: Session, user: models.User, delta: int) -> models.User:
    user.credits += delta
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def log_api_call(db: Session, user: models.User, endpoint: str, cost: int) -> models.APICall:
    call = models.APICall(user_id=user.id, endpoint=endpoint, cost=cost)
    db.add(call)
    db.commit()
    db.refresh(call)
    return call
