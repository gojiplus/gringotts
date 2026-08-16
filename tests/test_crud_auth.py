from sqlalchemy import func

from gringotts import auth, cli, crud, models


def ledger_sum(db, user_id):
    return (
        db.query(func.sum(models.CreditTransaction.amount))
        .filter_by(user_id=user_id)
        .scalar()
        or 0
    )


def test_auth_hash_and_verify():
    key = auth.generate_api_key()
    assert key.startswith("gk_")
    hashed = auth.get_api_key_hash(key)
    assert auth.verify_api_key(key, hashed)
    assert not auth.verify_api_key("wrong", hashed)


def test_create_user_records_last4_and_initial_grant(db_session):
    user, key = auth.create_user_with_key(db_session, "carol", credits=5)
    assert user.key_last4 == key[-4:]
    assert user.credits == 5
    assert ledger_sum(db_session, user.id) == 5


def test_get_user_by_api_key(db_session):
    user, key = auth.create_user_with_key(db_session, "carol", credits=1)
    fetched = crud.get_user_by_api_key(db_session, key)
    assert fetched is not None
    assert fetched.id == user.id
    assert crud.get_user_by_api_key(db_session, "gk_nope") is None


def test_charge_user_atomic_with_ledger(db_session):
    user, _ = auth.create_user_with_key(db_session, "dave", credits=3)
    assert crud.charge_user(db_session, user, 2, endpoint="/x") is True
    assert user.credits == 1
    assert crud.charge_user(db_session, user, 5, endpoint="/x") is False
    db_session.refresh(user)
    assert user.credits == 1
    assert ledger_sum(db_session, user.id) == user.credits
    charge_rows = (
        db_session.query(models.CreditTransaction)
        .filter_by(user_id=user.id, kind="charge")
        .all()
    )
    assert len(charge_rows) == 1
    assert charge_rows[0].amount == -2
    assert charge_rows[0].endpoint == "/x"


def test_grant_credits_idempotent_by_external_id(db_session):
    user, _ = auth.create_user_with_key(db_session, "erin", credits=0)
    assert crud.grant_credits(
        db_session, user, 100, kind="purchase", external_id="evt_1"
    )
    assert user.credits == 100
    assert not crud.grant_credits(
        db_session, user, 100, kind="purchase", external_id="evt_1"
    )
    db_session.refresh(user)
    assert user.credits == 100
    assert ledger_sum(db_session, user.id) == 100


def test_refund_user(db_session):
    user, _ = auth.create_user_with_key(db_session, "frank", credits=3)
    crud.charge_user(db_session, user, 2, endpoint="/y")
    crud.refund_user(db_session, user, 2, endpoint="/y")
    assert user.credits == 3
    assert ledger_sum(db_session, user.id) == 3


def test_cli_create_add_balance(db_session, capsys):
    cli.main(["create-user", "gina", "--credits", "2"])
    out = capsys.readouterr().out
    assert "API key (shown once" in out
    assert "gk_" in out

    user = crud.get_user_by_username(db_session, "gina")
    assert user is not None
    assert user.credits == 2

    cli.main(["add-credits", "gina", "3"])
    db_session.expire_all()
    user = crud.get_user_by_username(db_session, "gina")
    assert user.credits == 5
    assert ledger_sum(db_session, user.id) == 5

    cli.main(["balance", "gina"])
    assert "5 credits" in capsys.readouterr().out
