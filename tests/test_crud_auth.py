import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from gringotts import auth, cli, crud, db, models


@pytest.fixture
def db_session(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db.Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(db, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(cli, "db", db)
    monkeypatch.setattr(cli.db, "SessionLocal", TestingSessionLocal)

    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_auth_hash_and_verify():
    key = auth.generate_api_key()
    hashed = auth.get_api_key_hash(key)
    assert auth.verify_api_key(key, hashed)
    assert not auth.verify_api_key("wrong", hashed)


def test_get_user_by_api_key(db_session):
    user, key = auth.create_user_with_key(db_session, "carol", credits=1)
    fetched = crud.get_user_by_api_key(db_session, key)
    assert fetched.id == user.id


def test_deduct_user_credits(db_session):
    user, _ = auth.create_user_with_key(db_session, "dave", credits=3)
    assert crud.deduct_user_credits(db_session, user, 2)
    assert user.credits == 1
    assert not crud.deduct_user_credits(db_session, user, 5)
    db_session.refresh(user)
    assert user.credits == 1


def test_cli_helpers(db_session):
    cli.create_user("erin", credits=2)
    user = db_session.query(models.User).filter_by(username="erin").first()
    assert user.credits == 2
    cli.add_credits("erin", 3)
    db_session.expire_all()
    user = db_session.query(models.User).filter_by(username="erin").first()
    assert user.credits == 5
