import os
from concurrent.futures import ThreadPoolExecutor

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

import gringotts
from gringotts import CreditedUser, GringottsConfig, auth, charge, crud, models
from gringotts.db import Base


def make_app():
    app = FastAPI()
    gringotts.init_app(
        app, GringottsConfig(idempotency_replay_validator=lambda _scope: True)
    )

    @app.get("/hello")
    def hello(user: CreditedUser = Depends(charge(2))):
        return {"msg": "world", "credits": user.credits}

    @app.get("/boom")
    def boom(user: CreditedUser = Depends(charge(2))):
        raise RuntimeError("kaboom")

    @app.get("/units")
    def units(
        user: CreditedUser = Depends(
            charge(lambda r: int(r.headers.get("X-Units", "1")))
        ),
    ):
        return {"credits": user.credits}

    return app


def test_charge_deducts_and_writes_ledger(db_session):
    user, key = auth.create_user_with_key(db_session, "alice", credits=5)
    client = TestClient(make_app())

    res = client.get("/hello", headers={"X-API-Key": key})
    assert res.status_code == 200
    assert res.json() == {"msg": "world", "credits": 3}

    db_session.refresh(user)
    assert user.credits == 3
    rows = db_session.query(models.CreditTransaction).filter_by(kind="charge").all()
    assert len(rows) == 1
    assert rows[0].endpoint == "/hello"
    assert rows[0].amount == -2


def test_missing_or_invalid_key_is_401(db_session):
    client = TestClient(make_app())
    assert client.get("/hello").status_code == 401
    assert client.get("/hello", headers={"X-API-Key": "gk_bad"}).status_code == 401


def test_402_body_snapshot(db_session):
    _, key = auth.create_user_with_key(db_session, "bob", credits=1)
    client = TestClient(make_app())

    res = client.get("/hello", headers={"X-API-Key": key})
    assert res.status_code == 402
    assert res.json() == {
        "error": {
            "code": "insufficient_credits",
            "type": "payment_required",
            "message": "Insufficient credits: request costs 2, balance is 1",
        },
        "x402Version": 1,
        "cost": 2,
        "balance": 1,
        "accepts": [],
    }


def test_402_accepts_lists_purchase_url_when_stripe_configured(db_session):
    from gringotts import CreditPack

    app = FastAPI()
    gringotts.init_app(
        app,
        GringottsConfig(
            packs=[CreditPack(credits=10, price_cents=100, name="Mini")],
            stripe_secret_key="sk_test_x",
        ),
    )

    @app.get("/hello")
    def hello(user: CreditedUser = Depends(charge(2))):
        return {"msg": "world"}

    _, key = auth.create_user_with_key(db_session, "cara", credits=0)
    client = TestClient(app, base_url="http://example.com")
    res = client.get("/hello", headers={"X-API-Key": key})
    assert res.status_code == 402
    assert res.json()["accepts"] == [
        {"type": "stripe-checkout", "url": "http://example.com/gringotts/buy"}
    ]


def test_refund_on_handler_exception(db_session):
    user, key = auth.create_user_with_key(db_session, "dana", credits=5)
    client = TestClient(make_app(), raise_server_exceptions=False)

    res = client.get("/boom", headers={"X-API-Key": key})
    assert res.status_code == 500

    db_session.refresh(user)
    assert user.credits == 5
    kinds = [
        t.kind
        for t in db_session.query(models.CreditTransaction)
        .filter_by(user_id=user.id)
        .all()
    ]
    assert kinds.count("charge") == 1
    assert kinds.count("refund") == 1


def test_no_refund_on_success(db_session):
    _, key = auth.create_user_with_key(db_session, "ed", credits=5)
    client = TestClient(make_app())
    client.get("/hello", headers={"X-API-Key": key})
    refunds = (
        db_session.query(models.CreditTransaction).filter_by(kind="refund").count()
    )
    assert refunds == 0


def test_callable_cost(db_session):
    user, key = auth.create_user_with_key(db_session, "fay", credits=10)
    client = TestClient(make_app())
    res = client.get("/units", headers={"X-API-Key": key, "X-Units": "7"})
    assert res.status_code == 200
    db_session.refresh(user)
    assert user.credits == 3


def test_concurrent_charges_never_overspend(tmp_path):
    url = (
        os.getenv("GRINGOTTS_TEST_DATABASE_URL")
        or f"sqlite:///{tmp_path}/concurrency.db"
    )
    connect_args = (
        {"check_same_thread": False, "timeout": 30} if url.startswith("sqlite") else {}
    )
    engine = create_engine(url, connect_args=connect_args)
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    with session_local() as setup:
        user, _ = auth.create_user_with_key(setup, "hammer", credits=50)
        user_id = user.id

    def attempt(_):
        session = session_local()
        try:
            target = crud.get_user(session, user_id)
            return crud.charge_user(session, target, 1, endpoint="/t")
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(100)))

    assert sum(results) == 50

    with session_local() as check:
        final = crud.get_user(check, user_id)
        assert final.credits == 0
        total = (
            check.query(func.sum(models.CreditTransaction.amount))
            .filter_by(user_id=user_id)
            .scalar()
        )
        assert total == 0
    engine.dispose()
