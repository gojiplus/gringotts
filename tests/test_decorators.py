from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import gringotts
from gringotts import GringottsConfig, auth, models
from gringotts.decorators import requires_credits


def make_app():
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    @app.get("/hello")
    @requires_credits(cost=2)
    async def hello(request: Request):
        return {"msg": "world"}

    @app.get("/boom")
    @requires_credits(cost=2)
    def boom(request: Request):
        raise RuntimeError("kaboom")

    return app


def test_requires_credits_deducts_and_logs(db_session):
    user, key = auth.create_user_with_key(db_session, "alice", credits=5)
    client = TestClient(make_app())

    res = client.get("/hello", headers={"X-API-Key": key})
    assert res.status_code == 200
    assert res.json() == {"msg": "world"}

    db_session.refresh(user)
    assert user.credits == 3
    rows = db_session.query(models.CreditTransaction).filter_by(kind="charge").all()
    assert len(rows) == 1
    assert rows[0].endpoint == "/hello"


def test_requires_credits_invalid_key(db_session):
    client = TestClient(make_app())
    assert client.get("/hello", headers={"X-API-Key": "bad"}).status_code == 401


def test_requires_credits_insufficient_credits(db_session):
    _, key = auth.create_user_with_key(db_session, "bob", credits=1)
    client = TestClient(make_app())

    res = client.get("/hello", headers={"X-API-Key": key})
    assert res.status_code == 402
    assert res.json()["error"]["code"] == "insufficient_credits"


def test_requires_credits_refunds_on_exception(db_session):
    user, key = auth.create_user_with_key(db_session, "carl", credits=5)
    client = TestClient(make_app(), raise_server_exceptions=False)

    res = client.get("/boom", headers={"X-API-Key": key})
    assert res.status_code == 500
    db_session.refresh(user)
    assert user.credits == 5
