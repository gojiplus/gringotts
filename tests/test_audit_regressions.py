"""Regression tests for the money-correctness audit findings (C1-C7).

Each test fails if its fix is reverted. They assert the *fixed* behavior;
the docstring names the finding and the failure the fix prevents.
"""

import json

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func
from test_router import WEBHOOK_SECRET, sign
from test_router import make_app as make_stripe_app

import gringotts
from gringotts import (
    CreditedUser,
    CreditPack,
    GringottsConfig,
    auth,
    charge,
    crud,
    models,
)


def ledger_sum(db, user_id):
    return (
        db.query(func.sum(models.CreditTransaction.amount))
        .filter_by(user_id=user_id)
        .scalar()
        or 0
    )


# ---- C1: negative cost must not mint credits -------------------------------
def test_c1_negative_callable_cost_rejected(db_session):
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    @app.get("/units")
    def units(
        user: CreditedUser = Depends(
            charge(lambda r: int(r.headers.get("X-Units", "1")))
        ),
    ):
        return {"credits": user.credits}

    user, key = auth.create_user_with_key(db_session, "mallory", credits=5)
    res = TestClient(app).get("/units", headers={"X-API-Key": key, "X-Units": "-1000"})
    assert res.status_code == 400
    db_session.refresh(user)
    assert user.credits == 5
    assert ledger_sum(db_session, user.id) == 5


def test_c1_charge_user_rejects_negative(db_session):
    user, _ = auth.create_user_with_key(db_session, "m2", credits=5)
    with pytest.raises(ValueError, match="cost cannot be negative"):
        crud.charge_user(db_session, user, -10)


# ---- C4: charge() must not touch the host's session ------------------------
def test_c4_refund_survives_host_session_error(db_session):
    """Host handler poisons its own request session; the refund must still land."""
    from gringotts.db import get_session

    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    @app.get("/dbboom")
    def dbboom(
        user: CreditedUser = Depends(charge(2)),
        db=Depends(get_session),
    ):
        db.add(models.User(username="dupe", api_key_hash="x"))
        db.add(models.User(username="dupe", api_key_hash="y"))  # will fail on flush
        db.flush()
        return {"ok": True}

    user, key = auth.create_user_with_key(db_session, "norma", credits=5)
    res = TestClient(app, raise_server_exceptions=False).get(
        "/dbboom", headers={"X-API-Key": key}
    )
    assert res.status_code == 500
    db_session.expire_all()
    user = db_session.get(models.User, user.id)
    assert user.credits == 5  # charged then refunded despite the host's DB error
    kinds = [t.kind for t in user.transactions]
    assert kinds.count("charge") == 1
    assert kinds.count("refund") == 1


def test_c4_charge_does_not_commit_host_pending_work(db_session):
    from gringotts.db import get_session

    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    def host_dep(db=Depends(get_session)):
        db.add(models.User(username="draft-row", api_key_hash="draft"))
        return db

    @app.get("/withdep")
    def withdep(
        db=Depends(host_dep),
        user: CreditedUser = Depends(charge(1)),
    ):
        raise RuntimeError("host aborts; its draft must not persist")

    _, key = auth.create_user_with_key(db_session, "olga", credits=5)
    TestClient(app, raise_server_exceptions=False).get(
        "/withdep", headers={"X-API-Key": key}
    )
    db_session.expire_all()
    assert (
        db_session.query(models.User).filter_by(username="draft-row").one_or_none()
        is None
    )


# ---- C5: non-positive grant amounts rejected -------------------------------
def test_c5_admin_grant_rejects_negative(db_session):
    _, admin_key = auth.create_user_with_key(
        db_session, "root", credits=0, is_admin=True
    )
    victim, _ = auth.create_user_with_key(db_session, "vic", credits=10)
    res = TestClient(make_stripe_app()).post(
        f"/gringotts/admin/users/{victim.id}/grant",
        data={"amount": "-50"},
        headers={"X-API-Key": admin_key},
    )
    assert res.status_code == 400
    db_session.refresh(victim)
    assert victim.credits == 10


def test_c5_credit_pack_rejects_nonpositive():
    with pytest.raises(ValueError, match="credits must be positive"):
        CreditPack(credits=0, price_cents=500, name="Bad")
    with pytest.raises(ValueError, match="credits must be positive"):
        CreditPack(credits=-5, price_cents=500, name="Bad")


def test_c5_grant_credits_rejects_negative(db_session):
    user, _ = auth.create_user_with_key(db_session, "p", credits=0)
    with pytest.raises(ValueError, match="amount cannot be negative"):
        crud.grant_credits(db_session, user, -100)


# ---- C2 / C3 / C6: webhook trust boundary ----------------------------------
def _event(user_id, credits, event_id, session_id, payment_status="paid"):
    obj = {
        "id": session_id,
        "object": "checkout.session",
        "amount_total": 500,
        "metadata": {"gringotts_user_id": str(user_id), "credits": str(credits)},
    }
    if payment_status is not None:
        obj["payment_status"] = payment_status
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "api_version": "2024-06-20",
            "type": "checkout.session.completed",
            "data": {"object": obj},
        }
    ).encode()


def _post(client, payload):
    return client.post(
        "/gringotts/webhook",
        content=payload,
        headers={"stripe-signature": sign(payload, WEBHOOK_SECRET)},
    )


def test_c2_unpaid_session_not_credited(db_session):
    user, _ = auth.create_user_with_key(db_session, "async", credits=0)
    payload = _event(user.id, 100, "evt_u", "cs_u", payment_status="unpaid")
    assert _post(TestClient(make_stripe_app()), payload).status_code == 200
    db_session.refresh(user)
    assert user.credits == 0
    assert db_session.query(models.CreditTransaction).count() == 0


def test_c3_duplicate_events_one_session_credit_once(db_session):
    user, _ = auth.create_user_with_key(db_session, "buyer", credits=0)
    client = TestClient(make_stripe_app())
    assert _post(client, _event(user.id, 100, "evt_A", "cs_same")).status_code == 200
    assert _post(client, _event(user.id, 100, "evt_B", "cs_same")).status_code == 200
    db_session.refresh(user)
    assert user.credits == 100  # one session -> credited once
    assert (
        db_session.query(models.CreditTransaction).filter_by(kind="purchase").count()
        == 1
    )


def test_c2_async_succeeded_credits(db_session):
    """The settlement event for a delayed payment does credit."""
    user, _ = auth.create_user_with_key(db_session, "ach", credits=0)
    obj = {
        "id": "cs_ach",
        "object": "checkout.session",
        "amount_total": 500,
        "payment_status": "paid",
        "metadata": {"gringotts_user_id": str(user.id), "credits": "100"},
    }
    payload = json.dumps(
        {
            "id": "evt_settle",
            "object": "event",
            "api_version": "2024-06-20",
            "type": "checkout.session.async_payment_succeeded",
            "data": {"object": obj},
        }
    ).encode()
    assert _post(TestClient(make_stripe_app()), payload).status_code == 200
    db_session.refresh(user)
    assert user.credits == 100


def test_c6_unknown_user_asks_stripe_to_retry(db_session, caplog):
    # A paid event for a missing user must NOT be silently 200'd (that drops the
    # payment forever); return non-2xx so Stripe retries the transient case.
    payload = _event(999999, 100, "evt_ghost", "cs_ghost")
    with caplog.at_level("ERROR", logger="gringotts.router"):
        assert _post(TestClient(make_stripe_app()), payload).status_code == 503
    assert db_session.query(models.CreditTransaction).count() == 0
    assert any("not found" in r.message for r in caplog.records)


# ---- agy F-02: charge() refund must not commit a handler's dirty user state --
def test_f02_handler_mutation_not_committed_by_refund(db_session):
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    @app.get("/evil")
    def evil(user: CreditedUser = Depends(charge(1))):
        user.is_admin = True  # aborted privilege escalation
        raise RuntimeError("handler aborts after mutating the yielded user")

    user, key = auth.create_user_with_key(db_session, "victim", credits=5)
    TestClient(app, raise_server_exceptions=False).get(
        "/evil", headers={"X-API-Key": key}
    )
    db_session.expire_all()
    user = db_session.get(models.User, user.id)
    assert user.is_admin is False  # mutation discarded, not committed by refund
    assert user.credits == 5  # charged then refunded
    kinds = [t.kind for t in user.transactions]
    assert kinds.count("charge") == 1
    assert kinds.count("refund") == 1


# ---- agy F-05: zero-cost failed request writes no phantom refund row --------
def test_f05_zero_cost_failure_writes_no_refund_row(db_session):
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    @app.get("/free")
    def free(
        user: CreditedUser = Depends(
            charge(lambda r: int(r.headers.get("X-Units", "0")))
        ),
    ):
        raise RuntimeError("free endpoint fails")

    _, key = auth.create_user_with_key(db_session, "z2", credits=5)
    TestClient(app, raise_server_exceptions=False).get(
        "/free", headers={"X-API-Key": key, "X-Units": "0"}
    )
    # only the initial grant exists; no phantom charge or refund row
    rows = db_session.query(models.CreditTransaction).all()
    assert [r.kind for r in rows] == ["grant"]


# ---- agy F-03: non-positive credits in webhook metadata is refused ----------
def test_f03_webhook_nonpositive_credits_refused(db_session):
    client = TestClient(make_stripe_app())
    # negative would 500 into grant_credits' ValueError without the guard
    neg = _event(1, -100, "evt_neg", "cs_neg")
    assert _post(client, neg).status_code == 200
    # zero would mint a $X-for-0-credits purchase row without the guard
    user, _ = auth.create_user_with_key(db_session, "zc", credits=0)
    zero = _event(user.id, 0, "evt_zero", "cs_zero")
    assert _post(client, zero).status_code == 200
    db_session.refresh(user)
    assert user.credits == 0
    assert (
        db_session.query(models.CreditTransaction).filter_by(kind="purchase").count()
        == 0
    )


# ---- C7: grant_credits only swallows genuine duplicates --------------------
def test_c7_grant_reraises_non_duplicate_integrity_error(db_session, monkeypatch):
    user, _ = auth.create_user_with_key(db_session, "q", credits=0)
    from sqlalchemy.exc import IntegrityError

    def boom():
        raise IntegrityError("boom", None, Exception("not a duplicate"))

    monkeypatch.setattr(db_session, "commit", boom)
    with pytest.raises(IntegrityError):
        crud.grant_credits(db_session, user, 50, external_id="brand_new_id")


# ---- review: client disconnect (CancelledError) must refund ----------------
def test_cancellederror_refunds_but_normal_close_does_not(db_session):
    import asyncio
    from types import SimpleNamespace

    user, key = auth.create_user_with_key(db_session, "cx", credits=10)

    def refunds():
        return (
            db_session.query(models.CreditTransaction)
            .filter_by(user_id=user.id, kind="refund")
            .count()
        )

    def req():
        return SimpleNamespace(
            headers={"X-API-Key": key}, url=SimpleNamespace(path="/x")
        )

    dep = charge(3)
    gen = dep(req())
    next(gen)  # charges, yields
    with pytest.raises(asyncio.CancelledError):
        gen.throw(asyncio.CancelledError())
    assert refunds() == 1  # client disconnect refunded

    gen2 = dep(req())
    next(gen2)
    gen2.close()  # GeneratorExit on normal close
    assert refunds() == 1  # no spurious refund


# ---- review: no double-credit across the pre-0.2 upgrade boundary ----------
def test_legacy_event_id_purchase_not_double_credited(db_session):
    # simulate a v0.1 purchase row keyed on the Stripe event id
    user, _ = auth.create_user_with_key(db_session, "legacy", credits=0)
    db_session.add(
        models.CreditTransaction(
            user_id=user.id,
            amount=100,
            kind="purchase",
            external_id="evt_legacy",  # old scheme: event id, not session id
            balance_after=100,
        )
    )
    db_session.query(models.User).filter_by(id=user.id).update(
        {models.User.credits: 100}
    )
    db_session.commit()
    # Stripe re-delivers the same event after upgrade; new code keys on session id
    payload = _event(user.id, 100, "evt_legacy", "cs_new")
    assert _post(TestClient(make_stripe_app()), payload).status_code == 200
    db_session.refresh(user)
    assert user.credits == 100  # not double-credited
    assert (
        db_session.query(models.CreditTransaction).filter_by(kind="purchase").count()
        == 1
    )


# ---- reconciliation precaution ---------------------------------------------
def test_reconcile_detects_divergence(db_session):
    user, _ = auth.create_user_with_key(db_session, "r", credits=10)
    assert crud.find_balance_discrepancies(db_session) == []
    db_session.query(models.User).filter_by(id=user.id).update(
        {models.User.credits: 999}
    )
    db_session.commit()
    disc = crud.find_balance_discrepancies(db_session)
    assert len(disc) == 1
    assert disc[0]["cached"] == 999
    assert disc[0]["ledger"] == 10
