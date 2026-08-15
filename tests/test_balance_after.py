"""balance_after running-balance: every row carries the balance as of that row,
and the three-way reconcile (credits == SUM(amount) == latest balance_after) holds."""

import pytest
from sqlalchemy import func

from gringotts import auth, crud, models


def _rows(db, user_id):
    return (
        db.query(models.CreditTransaction)
        .filter_by(user_id=user_id)
        .order_by(models.CreditTransaction.id)
        .all()
    )


def test_balance_after_tracks_running_balance(db_session):
    user, _ = auth.create_user_with_key(db_session, "u", credits=10)  # grant 10
    crud.charge_user(db_session, user, 3)  # -> 7
    crud.refund_user(db_session, user, 1)  # -> 8
    crud.grant_credits(db_session, user, 5)  # -> 13

    running = 0
    for row in _rows(db_session, user.id):
        running += row.amount
        assert row.balance_after == running  # each row carries the running total

    db_session.refresh(user)
    total = (
        db_session.query(func.sum(models.CreditTransaction.amount))
        .filter_by(user_id=user.id)
        .scalar()
    )
    assert user.credits == 13
    assert total == 13
    assert _rows(db_session, user.id)[-1].balance_after == 13
    assert crud.find_balance_discrepancies(db_session) == []


def test_reconcile_detects_balance_after_corruption(db_session):
    user, _ = auth.create_user_with_key(db_session, "v", credits=10)
    crud.charge_user(db_session, user, 4)  # balance 6, last balance_after 6
    # corrupt only the latest balance_after; credits and SUM(amount) still agree
    latest = _rows(db_session, user.id)[-1]
    db_session.query(models.CreditTransaction).filter_by(id=latest.id).update(
        {models.CreditTransaction.balance_after: 999}
    )
    db_session.commit()
    disc = crud.find_balance_discrepancies(db_session)
    assert len(disc) == 1
    assert disc[0]["cached"] == 6
    assert disc[0]["ledger"] == 6
    assert disc[0]["balance_after"] == 999


def test_balance_after_check_constraint_blocks_negative(db_session):
    from sqlalchemy.exc import IntegrityError

    user, _ = auth.create_user_with_key(db_session, "w", credits=5)
    db_session.add(
        models.CreditTransaction(
            user_id=user.id, amount=-1, kind="charge", balance_after=-1
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
