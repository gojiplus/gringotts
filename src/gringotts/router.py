"""User-facing routes: balance, usage, account page, purchase flow, webhook."""

import html
import logging
from importlib.resources import files

import stripe
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from . import billing, crud, pages
from .config import GringottsConfig, format_money
from .db import get_session
from .dependencies import API_KEY_HEADER, authenticate

logger = logging.getLogger(__name__)

# checkout.session.completed can fire before a delayed payment (ACH, etc.)
# settles; async_payment_succeeded is the settlement event. Both carry the
# session with metadata, so we handle both and gate on payment_status.
_FULFILL_EVENTS = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
}
_PAID_STATUSES = {"paid", "no_payment_required"}

# Reversal events. A refund (possibly partial) claws back a proportional share
# of the granted credits; a dispute claws back on funds_withdrawn and re-credits
# on funds_reinstated. The "warning_*" dispute events move no funds, so ignore.
_REFUND_EVENTS = {"refund.created", "refund.updated"}
_DISPUTE_WITHDRAWN = "charge.dispute.funds_withdrawn"
_DISPUTE_REINSTATED = "charge.dispute.funds_reinstated"


def _round_proportional(value: int, numerator: int, denominator: int) -> int:
    """Return ``round(value * numerator / denominator)`` using exact integers."""
    if value < 0 or numerator < 0 or denominator <= 0:
        raise ValueError("proportional rounding requires non-negative values")
    quotient, remainder = divmod(value * numerator, denominator)
    twice_remainder = remainder * 2
    if twice_remainder > denominator or (
        twice_remainder == denominator and quotient % 2
    ):
        quotient += 1
    return quotient


def _expandable_id(value) -> str | None:
    """Normalize a Stripe expandable ID represented as a string or object."""
    if isinstance(value, str):
        return value or None
    try:
        id_ = value["id"]
    except (KeyError, TypeError):
        return None
    return id_ if isinstance(id_, str) and id_ else None


def _checkout_fields(session):
    """Extract the fields required to settle a Checkout Session."""
    metadata = session["metadata"]
    order_id = session["client_reference_id"]
    metadata_order_id = metadata["gringotts_order_id"]
    user_id = int(metadata["gringotts_user_id"])
    credits = int(metadata["credits"])
    session_id = session["id"]
    payment_status = session["payment_status"]
    amount_cents = int(session["amount_total"])
    raw_currency = session["currency"]
    currency = raw_currency.lower() if isinstance(raw_currency, str) else ""
    payment_intent_id = _expandable_id(session["payment_intent"])
    if (
        not isinstance(order_id, str)
        or not order_id
        or metadata_order_id != order_id
        or not isinstance(session_id, str)
        or not session_id
        or not isinstance(payment_status, str)
        or not payment_status
        or amount_cents < 0
        or len(currency) != 3
        or not currency.isalpha()
        or (amount_cents and not payment_intent_id)
    ):
        raise ValueError("invalid Checkout Session accounting fields")
    return (
        order_id,
        user_id,
        credits,
        session_id,
        payment_status,
        amount_cents,
        currency,
        payment_intent_id,
    )


def _checkout_order_marker(session, db: Session) -> str | None:
    """Return this library's order marker, or None for unrelated Checkout."""
    try:
        metadata_reference = session["metadata"]["gringotts_order_id"]
    except (KeyError, TypeError):
        metadata_reference = None
    if isinstance(metadata_reference, str) and metadata_reference:
        return metadata_reference
    try:
        reference = session["client_reference_id"]
    except (KeyError, TypeError):
        return None
    if (
        isinstance(reference, str)
        and reference
        and crud.get_checkout_order(db, reference) is not None
    ):
        # Our immutable event snapshot may be missing metadata. A reference only
        # identifies Gringotts after it resolves to an order persisted locally;
        # unrelated Checkout integrations can use their own client references.
        return reference
    return None


def _retrieve_checkout(session, event_id: str, stripe_secret_key: str | None):
    """Retrieve a Checkout Session whose immutable event snapshot is incomplete."""
    try:
        session_id = session["id"]
    except (KeyError, TypeError):
        session_id = None
    if not isinstance(session_id, str) or not session_id or not stripe_secret_key:
        logger.error(
            "gringotts webhook %s: Checkout Session cannot be retrieved", event_id
        )
        raise HTTPException(
            status_code=503, detail="Checkout data incomplete; retry later"
        )
    try:
        return stripe.checkout.Session.retrieve(session_id, api_key=stripe_secret_key)
    except stripe.StripeError as err:
        logger.warning(
            "gringotts webhook %s: could not retrieve Checkout Session %s",
            event_id,
            session_id,
        )
        raise HTTPException(
            status_code=503, detail="Checkout data incomplete; retry later"
        ) from err


def _apply_reversal(
    db, event, *, payment_intent, reversed_cents, external_id, endpoint
):
    """Claw back credits for a refund/dispute using cumulative delta math.

    The target total clawback is `round(granted * cumulative_reversed / paid)`,
    capped at the full payment; we deduct only the delta over what earlier events
    already clawed, so independent per-event rounding can never over- or
    under-claw. Idempotent on `external_id`. Raises 503 (retryable) when the
    purchase isn't found yet — Stripe does not guarantee delivery order, so the
    reversal may simply arrive before its checkout event.
    """
    if crud.external_id_exists(db, external_id):
        return  # already processed this reversal
    purchase = crud.find_purchase_by_payment_intent(db, payment_intent)
    if purchase is None:
        logger.warning(
            "gringotts webhook %s: reversal for payment_intent=%s with no matching "
            "purchase (out-of-order delivery, or a pre-0.3 purchase); asking Stripe "
            "to retry",
            event["id"],
            payment_intent,
        )
        raise HTTPException(status_code=503, detail="Purchase not found; retry later")
    if not purchase.amount_cents:
        logger.error(
            "gringotts webhook %s: purchase for payment_intent=%s has no amount; "
            "cannot compute a proportional clawback",
            event["id"],
            payment_intent,
        )
        return
    # Lock the user row before reading cumulative totals, so two concurrent
    # partial-refund events for the same purchase can't both read stale totals
    # and each under-claw — the read, delta calc, and write are serialized.
    user = crud.lock_user(db, purchase.user_id)
    if user is None:
        logger.error("gringotts webhook %s: reversal user missing", event["id"])
        raise HTTPException(status_code=503, detail="User not found; retry later")
    granted, paid = purchase.amount, purchase.amount_cents
    prior_cents, prior_clawed = crud.clawback_totals(db, payment_intent)
    capped_cents = min(prior_cents + reversed_cents, paid)
    target = _round_proportional(granted, capped_cents, paid)
    delta = max(0, target - prior_clawed)
    crud.clawback_credits(
        db,
        user,
        delta,
        external_id=external_id,
        endpoint=endpoint,
        amount_cents=reversed_cents,
        payment_intent_id=payment_intent,
        currency=purchase.currency,
    )


def _process_refund(db, event, *, stripe_secret_key: str | None) -> None:
    """Claw back credits for a settled Stripe refund (clamped at zero)."""
    refund = event["data"]["object"]
    try:
        payment_intent = _expandable_id(refund["payment_intent"])
        refund_id = refund["id"]
        refund_amount = int(refund["amount"])
        status = refund["status"]
    except (KeyError, TypeError, ValueError):
        payment_intent = refund_id = status = None
        refund_amount = 0
    if (
        not payment_intent
        or not isinstance(refund_id, str)
        or not refund_id
        or refund_amount <= 0
        or not isinstance(status, str)
        or not status
    ):
        try:
            original_refund_id = refund["id"]
        except (KeyError, TypeError):
            original_refund_id = None
        if (
            not isinstance(original_refund_id, str)
            or not original_refund_id
            or not stripe_secret_key
        ):
            logger.error("gringotts webhook %s: refund is incomplete", event["id"])
            raise HTTPException(
                status_code=503, detail="Refund data incomplete; retry later"
            )
        try:
            # Event payloads are immutable and retain the webhook endpoint's API
            # version. Retrieve the current object rather than retrying the same
            # incomplete snapshot forever.
            refund = stripe.Refund.retrieve(
                original_refund_id, api_key=stripe_secret_key
            )
            payment_intent = _expandable_id(refund["payment_intent"])
            refund_id = refund["id"]
            refund_amount = int(refund["amount"])
            status = refund["status"]
        except stripe.StripeError as err:
            logger.warning(
                "gringotts webhook %s: could not retrieve refund %s",
                event["id"],
                original_refund_id,
            )
            raise HTTPException(
                status_code=503, detail="Refund data incomplete; retry later"
            ) from err
        except (KeyError, TypeError, ValueError):
            payment_intent = refund_id = status = None
            refund_amount = 0
        if (
            not payment_intent
            or not isinstance(refund_id, str)
            or not refund_id
            or refund_amount <= 0
            or not isinstance(status, str)
            or not status
        ):
            logger.error(
                "gringotts webhook %s: retrieved refund %s is incomplete",
                event["id"],
                original_refund_id,
            )
            raise HTTPException(
                status_code=503, detail="Refund data incomplete; retry later"
            )
    if status != "succeeded":
        # A pending/failed/canceled refund hasn't returned money — wait for the
        # refund.updated that flips it to succeeded (no row written yet, so that
        # later event isn't deduped away).
        logger.info(
            "gringotts webhook %s: refund %s status=%s, not clawing yet",
            event["id"],
            refund_id,
            status,
        )
        return
    _apply_reversal(
        db,
        event,
        payment_intent=payment_intent,
        reversed_cents=refund_amount,
        external_id=refund_id,
        endpoint="stripe:refund",
    )


def _process_dispute(
    db, event, *, reinstate: bool, stripe_secret_key: str | None
) -> None:
    """Claw back on a withdrawn dispute, re-credit on a reinstated one."""
    dispute = event["data"]["object"]
    try:
        payment_intent = _expandable_id(dispute["payment_intent"])
        dispute_id = dispute["id"]
        dispute_amount = int(dispute["amount"])
    except (KeyError, TypeError, ValueError):
        payment_intent = dispute_id = None
        dispute_amount = 0
    if (
        not payment_intent
        or not isinstance(dispute_id, str)
        or not dispute_id
        or dispute_amount <= 0
    ):
        try:
            original_dispute_id = dispute["id"]
        except (KeyError, TypeError):
            original_dispute_id = None
        if (
            not isinstance(original_dispute_id, str)
            or not original_dispute_id
            or not stripe_secret_key
        ):
            logger.error("gringotts webhook %s: dispute is incomplete", event["id"])
            raise HTTPException(
                status_code=503, detail="Dispute data incomplete; retry later"
            )
        try:
            dispute = stripe.Dispute.retrieve(
                original_dispute_id, api_key=stripe_secret_key
            )
            payment_intent = _expandable_id(dispute["payment_intent"])
            dispute_id = dispute["id"]
            dispute_amount = int(dispute["amount"])
        except stripe.StripeError as err:
            logger.warning(
                "gringotts webhook %s: could not retrieve dispute %s",
                event["id"],
                original_dispute_id,
            )
            raise HTTPException(
                status_code=503, detail="Dispute data incomplete; retry later"
            ) from err
        except (KeyError, TypeError, ValueError):
            payment_intent = dispute_id = None
            dispute_amount = 0
        if (
            not payment_intent
            or not isinstance(dispute_id, str)
            or not dispute_id
            or dispute_amount <= 0
        ):
            logger.error(
                "gringotts webhook %s: retrieved dispute %s is incomplete",
                event["id"],
                original_dispute_id,
            )
            raise HTTPException(
                status_code=503, detail="Dispute data incomplete; retry later"
            )
    withdrawn_key = f"{dispute_id}:withdrawn"
    if not reinstate:
        _apply_reversal(
            db,
            event,
            payment_intent=payment_intent,
            reversed_cents=dispute_amount,
            external_id=withdrawn_key,
            endpoint="stripe:dispute",
        )
        return
    # Reinstatement: restore exactly what the withdrawal clawed back.
    if crud.external_id_exists(db, f"{dispute_id}:reinstated"):
        return  # already reinstated
    if not crud.external_id_exists(db, withdrawn_key):
        # Out-of-order: the withdrawal hasn't been processed yet. Retry so we
        # don't lose the reinstatement and later deduct permanently.
        logger.warning(
            "gringotts webhook %s: dispute %s reinstated before withdrawal seen; "
            "asking Stripe to retry",
            event["id"],
            dispute_id,
        )
        raise HTTPException(status_code=503, detail="Withdrawal not seen; retry later")
    purchase = crud.find_purchase_by_payment_intent(db, payment_intent)
    if purchase is None or not purchase.amount_cents:
        logger.error(
            "gringotts webhook %s: reinstate purchase missing or incomplete",
            event["id"],
        )
        raise HTTPException(status_code=503, detail="Purchase not found; retry later")
    # Lock the user before reading/writing, same as _apply_reversal, so a
    # concurrent refund and this reinstatement serialize on the cumulative math.
    user = crud.lock_user(db, purchase.user_id)
    if user is None:
        logger.error("gringotts webhook %s: reinstate user missing", event["id"])
        raise HTTPException(status_code=503, detail="User not found; retry later")
    # Restore only the amount above the target clawback for all *other* active
    # reversals. A refund can arrive between the dispute withdrawal and its
    # reinstatement; restoring the withdrawal's original deduction would erase
    # that refund's clawback and make the result depend on webhook order.
    prior_cents, prior_clawed = crud.clawback_totals(db, payment_intent)
    remaining_cents = max(0, prior_cents - dispute_amount)
    target = _round_proportional(
        purchase.amount,
        min(remaining_cents, purchase.amount_cents),
        purchase.amount_cents,
    )
    restored = max(0, prior_clawed - target)
    # Always record the reinstatement, even when restored is zero, so its negative
    # amount_cents cancels this dispute in future cumulative calculations.
    crud.grant_credits(
        db,
        user,
        restored,
        kind="reinstate",
        external_id=f"{dispute_id}:reinstated",
        amount_cents=-dispute_amount,
        payment_intent_id=payment_intent,
        currency=purchase.currency,
    )


_BUY_PAGE = """<!doctype html>
<html>
<head><meta charset="utf-8"><title>Buy credits</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 28rem;
          margin: 4rem auto; padding: 0 1rem; }}
  label {{ display: block; margin: 0.75rem 0; }}
  input[type=text] {{ width: 100%; padding: 0.5rem; }}
  button {{ margin-top: 1rem; padding: 0.6rem 1.2rem; font-size: 1rem; }}
</style></head>
<body>
<h1>Buy credits</h1>
{status}
<form method="post" action="{checkout_path}">
  <label>Your API key
    <input type="text" name="api_key" placeholder="gk_..." required>
  </label>
  {pack_inputs}
  <button type="submit">Continue to payment</button>
</form>
</body>
</html>"""


def _require_stripe(config: GringottsConfig) -> None:
    if not config.stripe_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "Stripe is not configured "
                "(set STRIPE_SECRET_KEY and define credit packs)"
            ),
        )


def build_router(config: GringottsConfig) -> APIRouter:
    """Build the user-facing router for the given configuration."""
    router = APIRouter()

    @router.get("/static/htmx.min.js", include_in_schema=False)
    def htmx_js():
        content = files("gringotts").joinpath("static/htmx.min.js").read_text()
        return Response(
            content,
            media_type="text/javascript",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @router.get("/balance")
    def balance(request: Request, db: Session = Depends(get_session)):
        user = authenticate(db, request.headers.get(API_KEY_HEADER))
        return {
            "username": user.username,
            "balance": user.credits,
            "key_last4": user.key_last4,
        }

    @router.get("/usage")
    def usage(
        request: Request,
        limit: int = 50,
        offset: int = 0,
        db: Session = Depends(get_session),
    ):
        user = authenticate(db, request.headers.get(API_KEY_HEADER))
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        transactions = crud.list_transactions(
            db, user_id=user.id, limit=limit, offset=offset
        )
        return {
            "balance": user.credits,
            "transactions": [
                {
                    "amount": t.amount,
                    "kind": t.kind,
                    "endpoint": t.endpoint,
                    "amount_cents": t.amount_cents,
                    "currency": t.currency,
                    "created_at": t.created_at.isoformat(),
                }
                for t in transactions
            ],
        }

    @router.get("/account", response_class=HTMLResponse)
    def account_page():
        body = (
            f'<div id="panel" hx-get="{config.mount_path}/account/panel"'
            ' hx-trigger="load, every 10s">'
            '<p class="muted">Paste your API key above.</p></div>'
        )
        return pages.shell("Your account", body, config.mount_path)

    @router.get("/account/panel", response_class=HTMLResponse)
    def account_panel(request: Request, db: Session = Depends(get_session)):
        user = authenticate(db, request.headers.get(API_KEY_HEADER))
        transactions = crud.list_transactions(db, user_id=user.id, limit=20)
        buy_link = (
            f'<p><a href="{config.mount_path}/buy">Buy more credits</a></p>'
            if config.stripe_enabled
            else ""
        )
        return (
            '<div class="tiles">'
            + pages.tile(str(user.credits), "credits remaining")
            + pages.tile(
                f"...{user.key_last4}", f"API key ({html.escape(user.username)})"
            )
            + "</div>"
            + buy_link
            + "<h2>Recent activity</h2>"
            + pages.usage_table(transactions)
        )

    @router.get("/buy", response_class=HTMLResponse)
    def buy_page(status: str | None = None):
        _require_stripe(config)
        pack_inputs = "\n".join(
            f'<label><input type="radio" name="pack" value="{i}"'
            f" {'checked' if i == 0 else ''}>"
            f" {html.escape(pack.name)} — {pack.credits} credits for "
            f"{format_money(pack.price_cents, pack.currency)}</label>"
            for i, pack in enumerate(config.packs)
        )
        status_html = ""
        if status == "success":
            status_html = (
                "<p><strong>Payment received — credits are on their way.</strong></p>"
            )
        elif status == "cancelled":
            status_html = "<p>Payment cancelled.</p>"
        checkout_path = f"{config.mount_path}/checkout"
        return _BUY_PAGE.format(
            status=status_html, checkout_path=checkout_path, pack_inputs=pack_inputs
        )

    @router.post("/checkout")
    def checkout(
        request: Request,
        api_key: str = Form(...),
        pack: int = Form(...),
        db: Session = Depends(get_session),
    ):
        _require_stripe(config)
        user = authenticate(db, api_key)
        if not 0 <= pack < len(config.packs):
            raise HTTPException(status_code=400, detail="Unknown credit pack")
        selected = config.packs[pack]
        order = crud.create_checkout_order(
            db,
            user,
            selected.credits,
            selected.price_cents,
            selected.currency,
        )
        session = billing.create_checkout_session(
            user, selected, config, str(request.base_url), order.id
        )
        session_id = getattr(session, "id", None)
        if not isinstance(session_id, str) or not session_id:
            raise HTTPException(
                status_code=502, detail="Stripe returned no Checkout Session ID"
            )
        crud.bind_checkout_order(db, order.id, session_id, commit=True)
        if not session.url:
            raise HTTPException(
                status_code=502, detail="Stripe returned no checkout URL"
            )
        return RedirectResponse(session.url, status_code=303)

    @router.post("/webhook")
    async def webhook(request: Request, db: Session = Depends(get_session)):
        if not config.stripe_webhook_secret:
            raise HTTPException(
                status_code=503, detail="Stripe webhook secret is not configured"
            )
        payload = await request.body()
        signature = request.headers.get("stripe-signature", "")
        try:
            event = stripe.Webhook.construct_event(
                payload, signature, config.stripe_webhook_secret
            )
        except (ValueError, stripe.SignatureVerificationError) as err:
            raise HTTPException(
                status_code=400, detail="Invalid webhook signature"
            ) from err

        if event["type"] in _FULFILL_EVENTS:
            session = event["data"]["object"]
            if _checkout_order_marker(session, db) is None:
                # The Stripe account may host unrelated Checkout integrations.
                # A valid Stripe signature alone does not authorize credits.
                logger.info(
                    "gringotts webhook %s: unrelated Checkout Session ignored",
                    event["id"],
                )
                return {"received": True}
            try:
                (
                    order_id,
                    user_id,
                    credits,
                    session_id,
                    payment_status,
                    amount_cents,
                    currency,
                    payment_intent_id,
                ) = _checkout_fields(session)
            except (KeyError, TypeError, ValueError):
                session = _retrieve_checkout(
                    session, event["id"], config.stripe_secret_key
                )
                try:
                    (
                        order_id,
                        user_id,
                        credits,
                        session_id,
                        payment_status,
                        amount_cents,
                        currency,
                        payment_intent_id,
                    ) = _checkout_fields(session)
                except (KeyError, TypeError, ValueError):
                    logger.error(
                        "gringotts webhook %s: retrieved Checkout Session is "
                        "incomplete",
                        event["id"],
                    )
                    raise HTTPException(
                        status_code=503,
                        detail="Checkout data incomplete; retry later",
                    ) from None
            try:
                order = crud.bind_checkout_order(db, order_id, session_id, commit=False)
            except ValueError as err:
                logger.error(
                    "gringotts webhook %s: Checkout order %s is bound elsewhere",
                    event["id"],
                    order_id,
                )
                raise HTTPException(
                    status_code=400, detail="Checkout order mismatch"
                ) from err
            if order is None:
                logger.error(
                    "gringotts webhook %s: Checkout order %s not found",
                    event["id"],
                    order_id,
                )
                raise HTTPException(
                    status_code=503, detail="Checkout order not found; retry later"
                )
            if (
                credits != order.credits
                or user_id != order.user_id
                or amount_cents != order.amount_cents
                or currency != order.currency
            ):
                db.rollback()
                logger.error(
                    "gringotts webhook %s: Checkout Session %s does not match "
                    "authorized order %s",
                    event["id"],
                    session_id,
                    order_id,
                )
                raise HTTPException(status_code=400, detail="Checkout order mismatch")
            if payment_status not in _PAID_STATUSES:
                # Not settled yet; wait for async_payment_succeeded.
                logger.info(
                    "gringotts webhook %s: payment_status=%s, deferring credit",
                    event["id"],
                    payment_status,
                )
                return {"received": True}
            user = crud.get_user(db, order.user_id)
            if user is None:
                # Usually transient (the user existed when checkout was created;
                # replica lag or a restore in flight). Return non-2xx so Stripe
                # retries — after its retry window a genuine deletion surfaces in
                # the dashboard instead of silently dropping the payment.
                logger.error(
                    "gringotts webhook %s: user %s not found; asking Stripe to retry",
                    event["id"],
                    order.user_id,
                )
                raise HTTPException(
                    status_code=503, detail="User not found; retry later"
                )
            # Idempotency is keyed on the checkout session, not the event id:
            # Stripe may emit several event objects for one session.
            granted = crud.grant_credits(
                db,
                user,
                order.credits,
                kind="purchase",
                external_id=session_id,
                amount_cents=amount_cents,
                payment_intent_id=payment_intent_id,
                currency=order.currency,
            )
            if not granted:
                logger.info(
                    "gringotts webhook: checkout session %s already credited",
                    session_id,
                )
        elif event["type"] in _REFUND_EVENTS:
            _process_refund(db, event, stripe_secret_key=config.stripe_secret_key)
        elif event["type"] == _DISPUTE_WITHDRAWN:
            _process_dispute(
                db,
                event,
                reinstate=False,
                stripe_secret_key=config.stripe_secret_key,
            )
        elif event["type"] == _DISPUTE_REINSTATED:
            _process_dispute(
                db,
                event,
                reinstate=True,
                stripe_secret_key=config.stripe_secret_key,
            )
        return {"received": True}

    return router
