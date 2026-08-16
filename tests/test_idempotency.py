"""Response-caching idempotency: a retried request applies at most once.

The mechanism is :class:`gringotts.idempotency.IdempotencyMiddleware`: the first
request with an ``Idempotency-Key`` runs and its response is stored; a later
request with the same key from the same caller gets that stored response back
without re-running the handler.
"""

from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from test_charge import make_app

import gringotts
from gringotts import CreditedUser, GringottsConfig, auth, charge, crud, models
from gringotts.idempotency import _fingerprint


def test_replay_returns_cached_response_and_charges_once(db_session):
    user, key = auth.create_user_with_key(db_session, "a", credits=10)
    client = TestClient(make_app())
    h = {"X-API-Key": key, "Idempotency-Key": "req-1"}
    r1 = client.get("/hello", headers=h)
    r2 = client.get("/hello", headers=h)  # retry, same key
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json() == r1.json()  # the stored response, byte-for-byte
    assert r1.json()["credits"] == 8  # first charge left 8
    assert r2.headers.get("idempotent-replayed") == "true"
    db_session.refresh(user)
    assert user.credits == 8  # charged once, not twice
    assert (
        db_session.query(models.CreditTransaction).filter_by(kind="charge").count() == 1
    )


def test_distinct_keys_each_charge(db_session):
    user, key = auth.create_user_with_key(db_session, "b", credits=10)
    client = TestClient(make_app())
    client.get("/hello", headers={"X-API-Key": key, "Idempotency-Key": "k1"})
    client.get("/hello", headers={"X-API-Key": key, "Idempotency-Key": "k2"})
    db_session.refresh(user)
    assert user.credits == 6  # two distinct charges of 2


def test_without_key_each_charges(db_session):
    user, key = auth.create_user_with_key(db_session, "c", credits=10)
    client = TestClient(make_app())
    client.get("/hello", headers={"X-API-Key": key})
    client.get("/hello", headers={"X-API-Key": key})
    db_session.refresh(user)
    assert user.credits == 6  # no key -> each request charges


def test_key_scoped_per_caller(db_session):
    # a shared key must not let caller B replay caller A's cached response
    ua, ka = auth.create_user_with_key(db_session, "ua", credits=10)
    ub, kb = auth.create_user_with_key(db_session, "ub", credits=10)
    client = TestClient(make_app())
    client.get("/hello", headers={"X-API-Key": ka, "Idempotency-Key": "shared"})
    client.get("/hello", headers={"X-API-Key": kb, "Idempotency-Key": "shared"})
    db_session.refresh(ua)
    db_session.refresh(ub)
    assert ua.credits == 8
    assert ub.credits == 8  # B was charged, not handed A's cached free response


def test_conflict_on_different_request(db_session):
    user, key = auth.create_user_with_key(db_session, "cf", credits=10)
    client = TestClient(make_app())
    assert (
        client.get(
            "/hello", headers={"X-API-Key": key, "Idempotency-Key": "k"}
        ).status_code
        == 200
    )
    # same key, different path (method+path+query+body fingerprint differs) -> 409
    r = client.get(
        "/units", headers={"X-API-Key": key, "Idempotency-Key": "k", "X-Units": "1"}
    )
    assert r.status_code == 409
    db_session.refresh(user)
    assert user.credits == 8  # only /hello (cost 2) applied


def test_key_pins_the_operation(db_session):
    # headers aren't part of the fingerprint: the key pins the whole operation to
    # the first request, so a same-path replay returns the first result verbatim.
    user, key = auth.create_user_with_key(db_session, "kp", credits=10)
    client = TestClient(make_app())
    h = {"X-API-Key": key, "Idempotency-Key": "u"}
    r1 = client.get("/units", headers={**h, "X-Units": "1"})  # cost 1
    r2 = client.get("/units", headers={**h, "X-Units": "9"})  # replay of r1
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    db_session.refresh(user)
    assert user.credits == 9  # only the first (cost 1) charge applied


def test_server_error_is_not_cached_and_retry_reattempts(db_session):
    user, key = auth.create_user_with_key(db_session, "rb", credits=10)
    client = TestClient(make_app(), raise_server_exceptions=False)
    h = {"X-API-Key": key, "Idempotency-Key": "b1"}
    assert client.get("/boom", headers=h).status_code == 500  # charged, raised, refund
    assert client.get("/boom", headers=h).status_code == 500  # re-attempted, not cached
    db_session.refresh(user)
    assert user.credits == 10  # each attempt charged then refunded -> net zero
    assert (
        db_session.query(models.CreditTransaction).filter_by(kind="charge").count() == 2
    )


def test_retry_after_failure_charges_once(db_session):
    # first keyed attempt fails (500, refunded, not cached); the retry that
    # succeeds is charged fresh — not replayed into free successful work.
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())
    calls = {"n": 0}

    @app.get("/flaky")
    def flaky(user: CreditedUser = Depends(charge(2))):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient failure")
        return {"ok": True}

    user, key = auth.create_user_with_key(db_session, "fk", credits=10)
    client = TestClient(app, raise_server_exceptions=False)
    h = {"X-API-Key": key, "Idempotency-Key": "flk"}
    assert client.get("/flaky", headers=h).status_code == 500  # fails, refunded
    assert client.get("/flaky", headers=h).status_code == 200  # succeeds, charged
    db_session.refresh(user)
    assert user.credits == 8  # paid once for the successful call, not free
    assert crud.find_balance_discrepancies(db_session) == []


def test_insufficient_credits_is_cached(db_session):
    _, key = auth.create_user_with_key(db_session, "cr", credits=1)
    client = TestClient(make_app())
    h = {"X-API-Key": key, "Idempotency-Key": "p"}
    r1 = client.get("/hello", headers=h)  # cost 2 > balance 1 -> 402
    r2 = client.get("/hello", headers=h)
    assert r1.status_code == 402
    assert r2.status_code == 402  # a 4xx is a deterministic result -> cached
    assert r2.headers.get("idempotent-replayed") == "true"
    assert r2.json() == r1.json()


def test_admin_grant_idempotent_via_middleware(db_session):
    from test_router import make_app as make_stripe_app

    _, admin_key = auth.create_user_with_key(
        db_session, "root", credits=0, is_admin=True
    )
    victim, _ = auth.create_user_with_key(db_session, "v", credits=0)
    client = TestClient(make_stripe_app())
    h = {"X-API-Key": admin_key, "Idempotency-Key": "admin-grant-1"}
    r1 = client.post(
        f"/gringotts/admin/users/{victim.id}/grant", data={"amount": "25"}, headers=h
    )
    r2 = client.post(
        f"/gringotts/admin/users/{victim.id}/grant", data={"amount": "25"}, headers=h
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.headers.get("idempotent-replayed") == "true"
    db_session.refresh(victim)
    assert victim.credits == 25  # granted once despite two identical requests


def test_oversized_key_is_rejected(db_session):
    _, key = auth.create_user_with_key(db_session, "big", credits=10)
    client = TestClient(make_app())
    h = {"X-API-Key": key, "Idempotency-Key": "x" * 300}
    assert client.get("/hello", headers=h).status_code == 400


def test_idempotency_can_be_disabled(db_session):
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig(idempotency_enabled=False))

    @app.get("/hello")
    def hello(user: CreditedUser = Depends(charge(2))):
        return {"credits": user.credits}

    user, key = auth.create_user_with_key(db_session, "off", credits=10)
    client = TestClient(app)
    h = {"X-API-Key": key, "Idempotency-Key": "k"}
    client.get("/hello", headers=h)
    client.get("/hello", headers=h)
    db_session.refresh(user)
    assert user.credits == 6  # both charged; middleware absent


def test_in_progress_request_conflicts(db_session):
    # a fresh in-flight record for the same (caller, key) holds off a duplicate
    user, key = auth.create_user_with_key(db_session, "ip", credits=10)
    db_session.add(
        models.IdempotencyRecord(
            api_key_hash=auth.get_api_key_hash(key),
            idempotency_key="ipk",
            request_fingerprint=_fingerprint("GET", "/hello", b"", b""),
            completed=False,
        )
    )
    db_session.commit()
    client = TestClient(make_app())
    r = client.get("/hello", headers={"X-API-Key": key, "Idempotency-Key": "ipk"})
    assert r.status_code == 409
    db_session.refresh(user)
    assert user.credits == 10  # not charged while the first is in flight


def test_stale_in_progress_is_reclaimed(db_session):
    # an in-flight record whose owner crashed (older than the TTL) is reclaimable
    user, key = auth.create_user_with_key(db_session, "st", credits=10)
    db_session.add(
        models.IdempotencyRecord(
            api_key_hash=auth.get_api_key_hash(key),
            idempotency_key="stk",
            request_fingerprint=_fingerprint("GET", "/hello", b"", b""),
            completed=False,
            created_at=datetime.now(UTC) - timedelta(seconds=200),
        )
    )
    db_session.commit()
    client = TestClient(make_app())
    r = client.get("/hello", headers={"X-API-Key": key, "Idempotency-Key": "stk"})
    assert r.status_code == 200  # reclaimed and ran
    db_session.refresh(user)
    assert user.credits == 8  # charged


def test_reconcile_clean_after_idempotent_ops(db_session):
    _, key = auth.create_user_with_key(db_session, "e", credits=10)
    client = TestClient(make_app())
    h = {"X-API-Key": key, "Idempotency-Key": "x"}
    client.get("/hello", headers=h)
    client.get("/hello", headers=h)
    db_session.expire_all()  # charge committed on the dependency's own session
    assert crud.find_balance_discrepancies(db_session) == []
