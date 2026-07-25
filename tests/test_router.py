import hashlib
import hmac
import json
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import gringotts
from gringotts import CreditPack, GringottsConfig, auth, models

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


def checkout_completed_event(user_id: int, credits: int, event_id: str = "evt_1") -> bytes:
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
                    "metadata": {
                        "gringotts_user_id": str(user_id),
                        "credits": str(credits),
                    },
                }
            },
        }
    ).encode()


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
        return SimpleNamespace(url="https://checkout.stripe.com/c/pay/test")

    monkeypatch.setattr("stripe.checkout.Session.create", fake_create)

    client = TestClient(make_app(), follow_redirects=False)
    res = client.post("/gringotts/checkout", data={"api_key": key, "pack": "1"})
    assert res.status_code == 303
    assert res.headers["location"] == "https://checkout.stripe.com/c/pay/test"
    assert captured["metadata"]["credits"] == "1000"
    assert captured["mode"] == "payment"
    assert captured["line_items"][0]["price_data"]["unit_amount"] == 4000


def test_checkout_rejects_bad_key_and_pack(db_session, monkeypatch):
    _, key = auth.create_user_with_key(db_session, "cara", credits=0)
    monkeypatch.setattr(
        "stripe.checkout.Session.create",
        lambda **kwargs: SimpleNamespace(url="https://example.com"),
    )
    client = TestClient(make_app(), follow_redirects=False)
    bad_key = client.post("/gringotts/checkout", data={"api_key": "gk_bad", "pack": "0"})
    assert bad_key.status_code == 401
    bad_pack = client.post("/gringotts/checkout", data={"api_key": key, "pack": "9"})
    assert bad_pack.status_code == 400


def test_webhook_credits_user(db_session):
    user, _ = auth.create_user_with_key(db_session, "dana", credits=0)
    payload = checkout_completed_event(user.id, 100)
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
    assert row.external_id == "evt_1"
    assert row.amount == 100
    assert row.amount_cents == 500


def test_webhook_is_idempotent(db_session):
    user, _ = auth.create_user_with_key(db_session, "ed", credits=0)
    payload = checkout_completed_event(user.id, 100)
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
    assert db_session.query(models.CreditTransaction).filter_by(kind="purchase").count() == 1


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


def test_webhook_ignores_other_events(db_session):
    payload = json.dumps(
        {"id": "evt_2", "object": "event", "type": "invoice.paid", "data": {"object": {}}}
    ).encode()
    client = TestClient(make_app())
    res = client.post(
        "/gringotts/webhook",
        content=payload,
        headers={"stripe-signature": sign(payload)},
    )
    assert res.status_code == 200
    assert db_session.query(models.CreditTransaction).count() == 0
