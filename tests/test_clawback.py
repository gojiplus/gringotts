"""Refund/dispute clawback: proportional, clamp-at-zero, idempotent, reversible."""

import json
import os
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from test_router import WEBHOOK_SECRET, make_app, sign

from gringotts import auth, crud, models
from gringotts.db import Base, make_engine
from gringotts.router import _round_proportional


def test_concurrent_partial_refunds_do_not_over_claw(tmp_path):
    # 7 credits for 10 cents; four concurrent 1-cent refunds. Correct cumulative
    # claw for 4 cents is round(7*4/10)=3; naive per-event rounding would claw
    # 1 each = 4. The per-user write lock must serialize the cumulative math on
    # both backends (FOR UPDATE is a no-op on SQLite).
    url = os.getenv("GRINGOTTS_TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/claw.db"
    engine = make_engine(url)
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with session_local() as s:
        user, _ = auth.create_user_with_key(s, "race", credits=0)
        uid = user.id
        s.add(
            models.CreditTransaction(
                user_id=uid,
                amount=7,
                kind="purchase",
                balance_after=7,
                amount_cents=10,
                payment_intent_id="pi_race",
            )
        )
        s.execute(
            models.User.__table__.update()
            .where(models.User.id == uid)
            .values(credits=7)
        )
        s.commit()

    def reverse(i):
        # mirrors router._apply_reversal's locked cumulative math
        s = session_local()
        try:
            u = crud.lock_user(s, uid)
            prior_cents, prior_clawed = crud.clawback_totals(s, "pi_race")
            capped = min(prior_cents + 1, 10)
            delta = max(0, round(7 * capped / 10) - prior_clawed)
            crud.clawback_credits(
                s,
                u,
                delta,
                external_id=f"re_{i}",
                amount_cents=1,
                payment_intent_id="pi_race",
            )
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(reverse, range(4)))

    with session_local() as c:
        assert crud.get_user(c, uid).credits == 4  # 7 - 3 clawed, not over-clawed
        assert crud.find_balance_discrepancies(c) == []
    engine.dispose()


def _client():
    return TestClient(make_app())


def _purchase_event(user_id, credits, pi, session_id="cs_p", event_id="evt_p"):
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "api_version": "2024-06-20",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "object": "checkout.session",
                    "amount_total": 500,
                    "payment_status": "paid",
                    "payment_intent": pi,
                    "metadata": {
                        "gringotts_user_id": str(user_id),
                        "credits": str(credits),
                    },
                }
            },
        }
    ).encode()


def _refund_event(pi, amount, refund_id="re_1", event_id="evt_r"):
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "type": "refund.created",
            "data": {
                "object": {
                    "id": refund_id,
                    "object": "refund",
                    "payment_intent": pi,
                    "amount": amount,
                    "status": "succeeded",
                }
            },
        }
    ).encode()


def _dispute_event(pi, etype, dispute_id="du_1", event_id="evt_d"):
    return json.dumps(
        {
            "id": event_id,
            "object": "event",
            "type": etype,
            "data": {
                "object": {
                    "id": dispute_id,
                    "object": "dispute",
                    "payment_intent": pi,
                    "amount": 500,
                }
            },
        }
    ).encode()


def _post(client, payload):
    return client.post(
        "/gringotts/webhook",
        content=payload,
        headers={"stripe-signature": sign(payload, WEBHOOK_SECRET)},
    )


def _buy(db, client, name, credits=100, pi="pi_1"):
    user, _ = auth.create_user_with_key(db, name, credits=0)
    assert _post(client, _purchase_event(user.id, credits, pi)).status_code == 200
    db.refresh(user)
    assert user.credits == credits
    return user


def test_purchase_stores_payment_intent(db_session):
    _buy(db_session, _client(), "p0")
    row = db_session.query(models.CreditTransaction).filter_by(kind="purchase").one()
    assert row.payment_intent_id == "pi_1"
    assert row.amount == 100
    assert row.amount_cents == 500


def test_proportional_rounding_is_exact_above_float_precision():
    credits = 9_007_199_254_740_993
    assert (
        _round_proportional(credits, 33_333_333, 100_000_000) == 3_002_399_721_556_333
    )
    assert round(credits * 33_333_333 / 100_000_000) != 3_002_399_721_556_333


def test_full_refund_claws_back_all(db_session):
    client = _client()
    user = _buy(db_session, client, "p1")  # 100 credits for 500 cents
    assert _post(client, _refund_event("pi_1", 500)).status_code == 200
    db_session.refresh(user)
    assert user.credits == 0
    assert crud.find_balance_discrepancies(db_session) == []


def test_partial_refund_is_proportional(db_session):
    client = _client()
    user = _buy(db_session, client, "p2")  # 100 credits for 500 cents
    assert _post(client, _refund_event("pi_1", 250)).status_code == 200  # half
    db_session.refresh(user)
    assert user.credits == 50  # round(100 * 250/500)
    assert crud.find_balance_discrepancies(db_session) == []


def test_clawback_clamps_at_zero(db_session):
    client = _client()
    user = _buy(db_session, client, "p3", credits=100)
    crud.charge_user(db_session, user, 80)  # balance now 20
    assert _post(client, _refund_event("pi_1", 500)).status_code == 200  # wants 100
    db_session.refresh(user)
    assert user.credits == 0  # clamped, not -80
    assert crud.find_balance_discrepancies(db_session) == []


def test_refund_is_idempotent(db_session):
    client = _client()
    user = _buy(db_session, client, "p4")
    payload = _refund_event("pi_1", 250, refund_id="re_dup")
    _post(client, payload)
    _post(client, payload)  # redelivery
    db_session.refresh(user)
    assert user.credits == 50  # clawed once
    assert (
        db_session.query(models.CreditTransaction).filter_by(kind="clawback").count()
        == 1
    )


def test_dispute_withdrawn_then_reinstated(db_session):
    client = _client()
    user = _buy(db_session, client, "p5")  # 100
    assert (
        _post(
            client, _dispute_event("pi_1", "charge.dispute.funds_withdrawn")
        ).status_code
        == 200
    )
    db_session.refresh(user)
    assert user.credits == 0
    assert (
        _post(
            client, _dispute_event("pi_1", "charge.dispute.funds_reinstated")
        ).status_code
        == 200
    )
    db_session.refresh(user)
    assert user.credits == 100  # restored
    assert crud.find_balance_discrepancies(db_session) == []


def test_dispute_warning_events_ignored(db_session):
    client = _client()
    user = _buy(db_session, client, "p6")
    # a warning/inquiry moves no funds; we register only funds_withdrawn/reinstated,
    # so an unhandled dispute event must not claw back
    assert (
        _post(client, _dispute_event("pi_1", "charge.dispute.created")).status_code
        == 200
    )
    db_session.refresh(user)
    assert user.credits == 100  # untouched


def test_refund_for_unmatched_purchase_asks_retry(db_session):
    # out-of-order delivery (or a pre-0.3 purchase): no match yet -> retryable
    client = _client()
    user = _buy(db_session, client, "p7")
    assert _post(client, _refund_event("pi_UNKNOWN", 500)).status_code == 503
    db_session.refresh(user)
    assert user.credits == 100  # nothing clawed


def test_multiple_partial_refunds_do_not_over_claw(db_session):
    # 5 credits for 3 cents; three 1-cent refunds. Independent rounding would
    # claw 2+2+2=6; cumulative delta math caps the total at 5.
    client = _client()
    user, _ = auth.create_user_with_key(db_session, "p8", credits=0)
    payload = json.dumps(
        {
            "id": "evt_small",
            "object": "event",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_small",
                    "object": "checkout.session",
                    "amount_total": 3,
                    "payment_status": "paid",
                    "payment_intent": "pi_small",
                    "metadata": {"gringotts_user_id": str(user.id), "credits": "5"},
                }
            },
        }
    ).encode()
    assert _post(client, payload).status_code == 200
    for i in range(3):
        assert (
            _post(client, _refund_event("pi_small", 1, refund_id=f"re_{i}")).status_code
            == 200
        )
    db_session.refresh(user)
    assert user.credits == 0  # exactly 5 clawed across the three refunds, not 6
    assert crud.find_balance_discrepancies(db_session) == []


def test_pending_refund_is_not_clawed_until_succeeded(db_session):
    client = _client()
    user = _buy(db_session, client, "p9")
    pending = json.loads(_refund_event("pi_1", 500).decode())
    pending["data"]["object"]["status"] = "pending"
    assert _post(client, json.dumps(pending).encode()).status_code == 200
    db_session.refresh(user)
    assert user.credits == 100  # not clawed while pending
    # the same refund flips to succeeded -> now claw
    succeeded = json.loads(_refund_event("pi_1", 500).decode())
    succeeded["type"] = "refund.updated"
    succeeded["data"]["object"]["status"] = "succeeded"
    assert _post(client, json.dumps(succeeded).encode()).status_code == 200
    db_session.refresh(user)
    assert user.credits == 0


def test_refund_without_status_asks_retry(db_session):
    client = _client()
    user = _buy(db_session, client, "p_missing_status")
    refund = json.loads(_refund_event("pi_1", 500).decode())
    del refund["data"]["object"]["status"]
    assert _post(client, json.dumps(refund).encode()).status_code == 503
    db_session.refresh(user)
    assert user.credits == 100


def test_dispute_is_proportional_after_partial_refund(db_session):
    client = _client()
    user = _buy(db_session, client, "pa")  # 100 credits for 500 cents
    assert _post(client, _refund_event("pi_1", 250)).status_code == 200  # claw 50
    # dispute the remaining 250 cents -> claw the other 50, total 100 (not 150)
    dispute = json.loads(
        _dispute_event("pi_1", "charge.dispute.funds_withdrawn").decode()
    )
    dispute["data"]["object"]["amount"] = 250
    assert _post(client, json.dumps(dispute).encode()).status_code == 200
    db_session.refresh(user)
    assert user.credits == 0  # 50 + 50, exactly the granted 100
    assert crud.find_balance_discrepancies(db_session) == []


def test_reinstate_before_withdrawal_asks_retry(db_session):
    client = _client()
    user = _buy(db_session, client, "pb")
    # reinstatement arrives before the withdrawal -> retryable, not lost
    assert (
        _post(
            client, _dispute_event("pi_1", "charge.dispute.funds_reinstated")
        ).status_code
        == 503
    )
    db_session.refresh(user)
    assert user.credits == 100


def test_refund_after_reinstated_dispute_claws_correctly(db_session):
    # withdraw (claw 100) -> reinstate (restore 100) must clear the cumulative,
    # so a later 250-cent refund claws its proportional 50, not zero.
    client = _client()
    user = _buy(db_session, client, "pd")  # 100 credits / 500 cents
    _post(client, _dispute_event("pi_1", "charge.dispute.funds_withdrawn"))
    _post(client, _dispute_event("pi_1", "charge.dispute.funds_reinstated"))
    db_session.refresh(user)
    assert user.credits == 100  # restored
    assert _post(client, _refund_event("pi_1", 250)).status_code == 200
    db_session.refresh(user)
    assert user.credits == 50  # proportional claw, not under-clawed to 100
    assert crud.find_balance_discrepancies(db_session) == []


def test_null_payment_intent_does_not_claw_unrelated_user(db_session):
    # a pre-0.3 purchase row (payment_intent_id NULL) must never be matched by a
    # reversal whose payment_intent is also null.
    user, _ = auth.create_user_with_key(db_session, "pe", credits=100)
    db_session.add(
        models.CreditTransaction(
            user_id=user.id,
            amount=100,
            kind="purchase",
            balance_after=100,
            amount_cents=500,
            payment_intent_id=None,  # legacy row
        )
    )
    db_session.commit()
    refund = json.loads(_refund_event("pi_x", 500).decode())
    refund["data"]["object"]["payment_intent"] = None
    assert _post(_client(), json.dumps(refund).encode()).status_code == 503
    db_session.refresh(user)
    assert user.credits == 100  # untouched


def test_zero_restore_reinstatement_prevents_later_over_claw(db_session):
    client = _client()
    user = _buy(db_session, client, "pf")  # 100 / 500
    crud.charge_user(db_session, user, 100)  # balance 0
    _post(client, _dispute_event("pi_1", "charge.dispute.funds_withdrawn"))  # claw 0
    _post(client, _dispute_event("pi_1", "charge.dispute.funds_reinstated"))  # offset
    crud.grant_credits(db_session, user, 100, external_id="topup")  # balance 100
    assert _post(client, _refund_event("pi_1", 250)).status_code == 200
    db_session.refresh(user)
    assert user.credits == 50  # proportional 50, not over-clawed to 0
    assert crud.find_balance_discrepancies(db_session) == []


def test_stats_net_clawbacks_from_purchased_and_revenue(db_session):
    client = _client()
    user = _buy(db_session, client, "pc")  # purchased 100 for 500 cents
    stats = crud.aggregate_stats(db_session)
    assert stats["credits_purchased"] == 100
    assert stats["revenue_cents"] == 500
    _post(client, _refund_event("pi_1", 500))  # full refund
    db_session.refresh(user)
    stats = crud.aggregate_stats(db_session)
    assert stats["credits_purchased"] == 0  # credits netted
    assert stats["revenue_cents"] == 0  # revenue netted
