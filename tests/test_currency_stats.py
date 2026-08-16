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
