import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import gringotts
from gringotts import CreditPack, GringottsConfig, auth, crud, models
from gringotts import db as gdb
from gringotts.db import Base, make_engine

WEBHOOK_SECRET = "whsec_testsecret"


def make_app():
    app = FastAPI()
    gringotts.init_app(
        app,
        GringottsConfig(
            packs=[
                CreditPack(credits=100, price_cents=500, name="Starter"),
                CreditPack(credits=1000, price_cents=4000, name="Pro"),
            ],
            stripe_secret_key="sk_test_x",
            stripe_webhook_secret=WEBHOOK_SECRET,
        ),
    )
    return app


def sign(payload: bytes, secret: str = WEBHOOK_SECRET) -> str:
    timestamp = int(time.time())
    signed = f"{timestamp}.".encode() + payload
    signature = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


def checkout_completed_event(
    user_id: int,
    credits: int,
    event_id: str = "evt_1",
    *,
    order_id: str | None = None,
) -> bytes:
    metadata = {
        "gringotts_user_id": str(user_id),
        "credits": str(credits),
    }
    if order_id is not None:
        metadata["gringotts_order_id"] = order_id
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "api_version": "2024-06-20",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_1",
                    "object": "checkout.session",
                    "amount_total": 500,
                    "currency": "usd",
                    "payment_status": "paid",
                    "payment_intent": "pi_test_1",
                    "client_reference_id": order_id,
                    "metadata": metadata,
                }
            },
        }
    ).encode()


def authorize_checkout(db_session, user_id, *, session_id="cs_test_1"):
    order = models.CheckoutOrder(
        id=f"order_{session_id}",
        stripe_session_id=session_id,
        user_id=user_id,
        credits=100,
        amount_cents=500,
        currency="usd",
    )
    db_session.add(order)
    db_session.commit()
    return order.id


def test_balance_endpoint(db_session):
    _, key = auth.create_user_with_key(db_session, "alice", credits=7)
    client = TestClient(make_app())

    res = client.get("/gringotts/balance", headers={"X-API-Key": key})
    assert res.status_code == 200
    body = res.json()
    assert body["username"] == "alice"
    assert body["balance"] == 7
    assert body["key_last4"] == key[-4:]

    assert client.get("/gringotts/balance").status_code == 401


def test_buy_page_lists_packs(db_session):
    client = TestClient(make_app())
    res = client.get("/gringotts/buy")
    assert res.status_code == 200
    assert "Starter" in res.text
    assert "Pro" in res.text


def test_buy_page_503_without_stripe(db_session):
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())
    assert TestClient(app).get("/gringotts/buy").status_code == 503


def test_checkout_redirects_to_stripe(db_session, monkeypatch):
    _, key = auth.create_user_with_key(db_session, "bob", credits=0)
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            id="cs_checkout_test", url="https://checkout.stripe.com/c/pay/test"
        )

    monkeypatch.setattr("stripe.checkout.Session.create", fake_create)

    client = TestClient(make_app(), follow_redirects=False)
    res = client.post("/gringotts/checkout", data={"api_key": key, "pack": "1"})
    assert res.status_code == 303
    assert res.headers["location"] == "https://checkout.stripe.com/c/pay/test"
    assert captured["metadata"]["credits"] == "1000"
    assert captured["metadata"]["gringotts_order_id"] == captured["client_reference_id"]
    assert captured["mode"] == "payment"
    assert captured["line_items"][0]["price_data"]["unit_amount"] == 4000
    order = db_session.query(models.CheckoutOrder).one()
    assert order.stripe_session_id == "cs_checkout_test"
    assert order.credits == 1000
    assert order.amount_cents == 4000
    assert order.currency == "usd"


def test_keyed_checkout_replays_form_authenticated_session(db_session, monkeypatch):
    _, key = auth.create_user_with_key(db_session, "checkout-retry", credits=0)
    _, other_key = auth.create_user_with_key(db_session, "checkout-other", credits=0)
    calls = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            id=f"cs_checkout_{len(calls)}",
            url=f"https://checkout.stripe.com/c/pay/{len(calls)}",
        )

    monkeypatch.setattr("stripe.checkout.Session.create", fake_create)
    client = TestClient(make_app(), follow_redirects=False)
    headers = {
        "Idempotency-Key": "checkout-attempt",
        "X-API-Key": other_key,
    }
    data = {"api_key": key, "pack": "0"}

    first = client.post("/gringotts/checkout", data=data, headers=headers)
    replay = client.post("/gringotts/checkout", data=data, headers=headers)

    assert first.status_code == 303
    assert replay.status_code == 303
    assert replay.headers.get("idempotent-replayed") == "true"
    assert replay.headers["location"] == first.headers["location"]
    assert len(calls) == 1
    assert db_session.query(models.CheckoutOrder).count() == 1
    record = db_session.query(models.IdempotencyRecord).one()
    assert record.api_key_hash == auth.get_api_key_hash(key)


def test_keyed_checkout_with_unparseable_form_never_runs(db_session, monkeypatch):
    _, key = auth.create_user_with_key(db_session, "checkout-fields", credits=0)
    calls = []
    monkeypatch.setattr(
        "stripe.checkout.Session.create", lambda **kwargs: calls.append(kwargs)
    )
    fields = [*(f"unused_{i}=x" for i in range(101)), f"api_key={key}", "pack=0"]
    response = TestClient(make_app()).post(
        "/gringotts/checkout",
        content="&".join(fields),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Idempotency-Key": "too-many-fields",
        },
    )

    assert response.status_code == 400
    assert calls == []
    assert db_session.query(models.CheckoutOrder).count() == 0
    assert db_session.query(models.IdempotencyRecord).count() == 0


def test_checkout_rejects_bad_key_and_pack(db_session, monkeypatch):
    _, key = auth.create_user_with_key(db_session, "cara", credits=0)
    monkeypatch.setattr(
        "stripe.checkout.Session.create",
        lambda **kwargs: SimpleNamespace(id="cs_unused", url="https://example.com"),
    )
    client = TestClient(make_app(), follow_redirects=False)
    bad_key = client.post(
        "/gringotts/checkout",
        data={"api_key": "gk_bad", "pack": "0"},
        headers={"Idempotency-Key": "invalid-checkout"},
    )
    assert bad_key.status_code == 401
    assert db_session.query(models.IdempotencyRecord).count() == 0
    bad_pack = client.post("/gringotts/checkout", data={"api_key": key, "pack": "9"})
    assert bad_pack.status_code == 400


def test_checkout_order_binds_to_only_one_session_concurrently(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path}/checkout-bind.db")
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(engine)
    with session_local() as setup:
        user, _ = auth.create_user_with_key(setup, "bind-race", credits=0)
        order = crud.create_checkout_order(setup, user, 100, 500, "usd")
        order_id = order.id

    def bind(session_id):
        with session_local() as session:
            try:
                crud.bind_checkout_order(session, order_id, session_id, commit=True)
            except ValueError:
                return False
            return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(bind, ("cs_a", "cs_b")))

    assert sum(outcomes) == 1
    with session_local() as check:
        assert check.get(models.CheckoutOrder, order_id).stripe_session_id in {
            "cs_a",
            "cs_b",
        }
    engine.dispose()


def test_webhook_credits_user(db_session):
    user, _ = auth.create_user_with_key(db_session, "dana", credits=0)
    order_id = authorize_checkout(db_session, user.id)
    payload = checkout_completed_event(user.id, 100, order_id=order_id)
    client = TestClient(make_app())

    res = client.post(
        "/gringotts/webhook",
        content=payload,
        headers={"stripe-signature": sign(payload)},
    )
    assert res.status_code == 200

    db_session.refresh(user)
    assert user.credits == 100
    row = db_session.query(models.CreditTransaction).filter_by(kind="purchase").one()
    # idempotency is keyed on the checkout session id, not the event id
    assert row.external_id == "cs_test_1"
    assert row.amount == 100
    assert row.amount_cents == 500
    assert row.currency == "usd"
    assert row.payment_intent_id == "pi_test_1"


def test_webhook_is_idempotent(db_session):
    user, _ = auth.create_user_with_key(db_session, "ed", credits=0)
    order_id = authorize_checkout(db_session, user.id)
    payload = checkout_completed_event(user.id, 100, order_id=order_id)
    client = TestClient(make_app())

    for _ in range(2):
        res = client.post(
            "/gringotts/webhook",
            content=payload,
            headers={"stripe-signature": sign(payload)},
        )
        assert res.status_code == 200

    db_session.refresh(user)
    assert user.credits == 100
    assert (
        db_session.query(models.CreditTransaction).filter_by(kind="purchase").count()
        == 1
    )


def test_same_checkout_session_fulfills_once_under_concurrent_webhooks(
    tmp_path, monkeypatch
):
    engine = make_engine(f"sqlite:///{tmp_path}/checkout-webhook-race.db")
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(gdb, "SessionLocal", session_local)
    with session_local() as setup:
        user, _ = auth.create_user_with_key(setup, "webhook-race", credits=0)
        order = crud.create_checkout_order(setup, user, 100, 500, "usd")
        crud.bind_checkout_order(setup, order.id, "cs_test_1", commit=True)
        user_id = user.id
        order_id = order.id

    payload = checkout_completed_event(user_id, 100, order_id=order_id)
    signature = sign(payload)

    def fulfill(_attempt):
        return (
            TestClient(make_app())
            .post(
                "/gringotts/webhook",
                content=payload,
                headers={"stripe-signature": signature},
            )
            .status_code
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(fulfill, range(2)))

    assert statuses == [200, 200]
    with session_local() as check:
        assert check.get(models.User, user_id).credits == 100
        purchases = (
            check.query(models.CreditTransaction).filter_by(kind="purchase").all()
        )
        assert len(purchases) == 1
        assert purchases[0].external_id == "cs_test_1"
        assert purchases[0].balance_after == 100
    engine.dispose()


def test_webhook_rejects_bad_signature(db_session):
    user, _ = auth.create_user_with_key(db_session, "fay", credits=0)
    payload = checkout_completed_event(user.id, 100)
    client = TestClient(make_app())

    res = client.post(
        "/gringotts/webhook",
        content=payload,
        headers={"stripe-signature": sign(payload, secret="whsec_wrong")},
    )
    assert res.status_code == 400
    db_session.refresh(user)
    assert user.credits == 0


def test_webhook_does_not_fulfill_an_unregistered_checkout(db_session):
    user, _ = auth.create_user_with_key(db_session, "unbound", credits=0)
    payload = checkout_completed_event(user.id, 1_000_000)

    response = TestClient(make_app()).post(
        "/gringotts/webhook",
        content=payload,
        headers={"stripe-signature": sign(payload)},
    )
    assert response.status_code == 200
    db_session.refresh(user)
    assert user.credits == 0
    assert (
        db_session.query(models.CreditTransaction).filter_by(kind="purchase").count()
        == 0
    )


def test_webhook_ignores_unrelated_checkout_with_its_own_client_reference(
    db_session, monkeypatch
):
    session = {
        "id": "cs_other_app",
        "object": "checkout.session",
        "amount_total": 500,
        "currency": "usd",
        "payment_status": "paid",
        "payment_intent": "pi_other_app",
        "client_reference_id": "other-app-cart-123",
        "metadata": {"other_app": "true"},
    }
    payload = json.dumps(
        {
            "id": "evt_other_app",
            "object": "event",
            "api_version": "2024-06-20",
            "type": "checkout.session.completed",
            "data": {"object": session},
        }
    ).encode()
    monkeypatch.setattr(
        "stripe.checkout.Session.retrieve", lambda *args, **kwargs: session
    )

    response = TestClient(make_app()).post(
        "/gringotts/webhook",
        content=payload,
        headers={"stripe-signature": sign(payload)},
    )

    assert response.status_code == 200
    assert db_session.query(models.CreditTransaction).count() == 0


def test_webhook_rejects_checkout_that_disagrees_with_authorized_order(db_session):
    user, _ = auth.create_user_with_key(db_session, "mismatch", credits=0)
    order_id = authorize_checkout(db_session, user.id)
    payload = checkout_completed_event(user.id, 1_000_000, order_id=order_id)

    response = TestClient(make_app()).post(
        "/gringotts/webhook",
        content=payload,
        headers={"stripe-signature": sign(payload)},
    )

    assert response.status_code == 400
    db_session.refresh(user)
    assert user.credits == 0
    assert (
        db_session.query(models.CreditTransaction).filter_by(kind="purchase").count()
        == 0
    )


def test_webhook_ignores_other_events(db_session):
    payload = json.dumps(
        {
            "id": "evt_2",
            "object": "event",
            "type": "invoice.paid",
            "data": {"object": {}},
        }
    ).encode()
    client = TestClient(make_app())
    res = client.post(
        "/gringotts/webhook",
        content=payload,
        headers={"stripe-signature": sign(payload)},
    )
    assert res.status_code == 200
    assert db_session.query(models.CreditTransaction).count() == 0
