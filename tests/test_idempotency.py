"""Idempotency-Key: a retried charge or grant applies at most once."""

from fastapi.testclient import TestClient
from test_charge import make_app

from gringotts import auth, crud, models


def test_charge_with_idempotency_key_charges_once(db_session):
    user, key = auth.create_user_with_key(db_session, "a", credits=10)
    client = TestClient(make_app())
    headers = {"X-API-Key": key, "Idempotency-Key": "req-1"}
    r1 = client.get("/hello", headers=headers)
    r2 = client.get("/hello", headers=headers)  # retry, same key
    assert r1.status_code == 200
    assert r2.status_code == 200
    db_session.refresh(user)
    assert user.credits == 8  # charged 2 once, not 4
    assert (
        db_session.query(models.CreditTransaction).filter_by(kind="charge").count() == 1
    )


def test_charge_distinct_keys_each_charge(db_session):
    user, key = auth.create_user_with_key(db_session, "b", credits=10)
    client = TestClient(make_app())
    client.get("/hello", headers={"X-API-Key": key, "Idempotency-Key": "k1"})
    client.get("/hello", headers={"X-API-Key": key, "Idempotency-Key": "k2"})
    db_session.refresh(user)
    assert user.credits == 6  # two distinct charges of 2


def test_charge_without_key_is_unchanged(db_session):
    user, key = auth.create_user_with_key(db_session, "c", credits=10)
    client = TestClient(make_app())
    client.get("/hello", headers={"X-API-Key": key})
    client.get("/hello", headers={"X-API-Key": key})
    db_session.refresh(user)
    assert user.credits == 6  # no key -> each request charges


def test_grant_credits_idempotent_on_key(db_session):
    user, _ = auth.create_user_with_key(db_session, "d", credits=0)
    assert crud.grant_credits(db_session, user, 50, idempotency_key="g1") is True
    assert crud.grant_credits(db_session, user, 50, idempotency_key="g1") is False
    db_session.refresh(user)
    assert user.credits == 50  # granted once


def test_admin_grant_idempotent_on_header(db_session):
    from test_router import make_app as make_stripe_app

    _, admin_key = auth.create_user_with_key(
        db_session, "root", credits=0, is_admin=True
    )
    victim, _ = auth.create_user_with_key(db_session, "v", credits=0)
    client = TestClient(make_stripe_app())
    headers = {"X-API-Key": admin_key, "Idempotency-Key": "admin-grant-1"}
    for _ in range(2):
        assert (
            client.post(
                f"/gringotts/admin/users/{victim.id}/grant",
                data={"amount": "25"},
                headers=headers,
            ).status_code
            == 200
        )
    db_session.refresh(victim)
    assert victim.credits == 25  # granted once despite two identical requests


def test_reconcile_clean_after_idempotent_ops(db_session):
    _, key = auth.create_user_with_key(db_session, "e", credits=10)
    client = TestClient(make_app())
    h = {"X-API-Key": key, "Idempotency-Key": "x"}
    client.get("/hello", headers=h)
    client.get("/hello", headers=h)
    db_session.expire_all()  # charges committed on the dependency's own session
    assert crud.find_balance_discrepancies(db_session) == []
