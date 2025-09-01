import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from gringotts import auth, crud, models, db, decorators


@pytest.fixture
def db_session(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db.Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(db, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(decorators, "SessionLocal", TestingSessionLocal)

    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def create_app():
    app = FastAPI()

    @app.get("/hello")
    @decorators.requires_credits(cost=2)
    async def hello(request: Request):
        return {"msg": "world"}

    return app


def test_requires_credits_deducts_and_logs(db_session):
    user, key = auth.create_user_with_key(db_session, "alice", credits=5)

    app = create_app()
    client = TestClient(app)

    res = client.get("/hello", headers={"X-API-Key": key})
    assert res.status_code == 200
    assert res.json() == {"msg": "world"}

    db_session.refresh(user)
    assert user.credits == 3

    calls = db_session.query(models.APICall).all()
    assert len(calls) == 1
    assert calls[0].endpoint == "/hello"
    assert calls[0].cost == 2


def test_requires_credits_invalid_key(db_session):
    app = create_app()
    client = TestClient(app)

    res = client.get("/hello", headers={"X-API-Key": "bad"})
    assert res.status_code == 401


def test_requires_credits_insufficient_credits(db_session):
    user, key = auth.create_user_with_key(db_session, "bob", credits=1)

    app = create_app()
    client = TestClient(app)

    res = client.get("/hello", headers={"X-API-Key": key})
    assert res.status_code == 402
