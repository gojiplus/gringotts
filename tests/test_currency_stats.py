"""Zero-decimal currency formatting and refund-net consumption stats."""

import pytest

from gringotts import auth, crud
from gringotts.config import CreditPack, GringottsConfig, format_money, is_zero_decimal


def test_mixed_currency_packs_rejected():
    with pytest.raises(ValueError, match="one currency"):
        GringottsConfig(
            packs=[
                CreditPack(credits=100, price_cents=500, name="usd", currency="usd"),
                CreditPack(credits=100, price_cents=500, name="eur", currency="eur"),
            ]
        )


def test_zero_decimal_currency_formatting():
    assert is_zero_decimal("jpy") is True
    assert is_zero_decimal("JPY") is True
    assert is_zero_decimal("usd") is False
    # JPY price_cents is whole yen, not hundredths
    assert format_money(500, "jpy") == "500 JPY"
    assert format_money(500, "usd") == "5.00 USD"
    assert format_money(4000, "eur") == "40.00 EUR"


def test_credits_consumed_nets_refunds(db_session):
    user, _ = auth.create_user_with_key(db_session, "s", credits=10)
    crud.charge_user(db_session, user, 4)  # consumed 4
    crud.charge_user(db_session, user, 3)  # consumed 7 gross
    crud.refund_user(db_session, user, 3)  # one charge reversed
    stats = crud.aggregate_stats(db_session)
    assert stats["credits_consumed"] == 4  # 7 charged - 3 refunded

    per_user = {u["username"]: u for u in crud.list_users_with_stats(db_session)}
    assert per_user["s"]["consumed"] == 4  # same netting per user


def test_fully_refunded_charge_is_zero_consumption(db_session):
    user, _ = auth.create_user_with_key(db_session, "t", credits=10)
    crud.charge_user(db_session, user, 5)
    crud.refund_user(db_session, user, 5)
    assert crud.aggregate_stats(db_session)["credits_consumed"] == 0


def test_revenue_is_aggregated_separately_by_currency(db_session):
    user, _ = auth.create_user_with_key(db_session, "multi-currency", credits=0)
    crud.grant_credits(
        db_session,
        user,
        100,
        kind="purchase",
        external_id="cs_usd",
        amount_cents=500,
        currency="usd",
    )
    crud.grant_credits(
        db_session,
        user,
        100,
        kind="purchase",
        external_id="cs_jpy",
        amount_cents=500,
        currency="jpy",
    )

    assert crud.aggregate_stats(db_session)["revenue_by_currency"] == {
        "jpy": 500,
        "usd": 500,
    }


@pytest.mark.parametrize("currency", ["us", "us1", "dollars", 123])
def test_monetary_ledger_rejects_invalid_currency(db_session, currency):
    user, _ = auth.create_user_with_key(db_session, f"bad-currency-{currency}")

    with pytest.raises((TypeError, ValueError), match="currency"):
        crud.grant_credits(
            db_session,
            user,
            100,
            kind="purchase",
            external_id=f"cs_{currency}",
            amount_cents=500,
            currency=currency,
        )

    db_session.refresh(user)
    assert user.credits == 0
    assert crud.list_transactions(db_session, user_id=user.id) == []
