"""Stripe Checkout session creation for credit packs."""

import stripe

from .config import CreditPack, GringottsConfig
from .models import User


def create_checkout_session(
    user: User, pack: CreditPack, config: GringottsConfig, base_url: str
) -> stripe.checkout.Session:
    """Create a one-time Stripe Checkout session for a credit pack.

    Uses inline price_data so no Stripe Dashboard product setup is needed; the
    metadata carries everything the webhook needs to credit the right user.
    """
    buy_url = f"{base_url}{config.mount_path.lstrip('/')}/buy"
    return stripe.checkout.Session.create(
        api_key=config.stripe_secret_key,
        mode="payment",
        line_items=[
            {
                "price_data": {
                    "currency": pack.currency,
                    "unit_amount": pack.price_cents,
                    "product_data": {"name": f"{pack.name} — {pack.credits} credits"},
                },
                "quantity": 1,
            }
        ],
        success_url=config.success_url or f"{buy_url}?status=success",
        cancel_url=config.cancel_url or f"{buy_url}?status=cancelled",
        metadata={"gringotts_user_id": str(user.id), "credits": str(pack.credits)},
        # copied onto the PaymentIntent and its Charge, so refund/dispute events
        # carry the user id even without the checkout session
        payment_intent_data={"metadata": {"gringotts_user_id": str(user.id)}},
    )
