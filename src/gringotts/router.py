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


def _process_refund(db, event) -> None:
    """Claw back credits proportional to a Stripe refund (clamped at zero)."""
    refund = event["data"]["object"]
    try:
        payment_intent_id = refund["payment_intent"]
        refund_id = refund["id"]
        refund_amount = int(refund["amount"])
    except (KeyError, TypeError, ValueError):
        logger.error("gringotts webhook %s: refund missing fields", event["id"])
        return
    purchase = crud.find_purchase_by_payment_intent(db, payment_intent_id)
    if purchase is None or not purchase.amount_cents:
        logger.warning(
            "gringotts webhook %s: refund for an unknown or pre-0.3 purchase "
            "(payment_intent=%s); cannot claw back",
            event["id"],
            payment_intent_id,
        )
        return
    user = crud.get_user(db, purchase.user_id)
    if user is None:
        logger.error("gringotts webhook %s: refund user missing", event["id"])
        return
    # proportional to the fraction of the payment refunded
    to_claw = round(purchase.amount * refund_amount / purchase.amount_cents)
    crud.clawback_credits(
        db, user, to_claw, external_id=refund_id, endpoint="stripe:refund"
    )


def _process_dispute(db, event, *, reinstate: bool) -> None:
    """Claw back on a lost/withdrawn dispute, re-credit on a reinstated one."""
    dispute = event["data"]["object"]
    try:
        payment_intent_id = dispute["payment_intent"]
        dispute_id = dispute["id"]
    except (KeyError, TypeError):
        logger.error("gringotts webhook %s: dispute missing fields", event["id"])
        return
    purchase = crud.find_purchase_by_payment_intent(db, payment_intent_id)
    if purchase is None:
        logger.warning(
            "gringotts webhook %s: dispute for an unknown or pre-0.3 purchase "
            "(payment_intent=%s); cannot act",
            event["id"],
            payment_intent_id,
        )
        return
    user = crud.get_user(db, purchase.user_id)
    if user is None:
        logger.error("gringotts webhook %s: dispute user missing", event["id"])
        return
    withdrawn_key = f"{dispute_id}:withdrawn"
    if reinstate:
        # restore exactly what was clawed back for this dispute (clamp-aware)
        restored = crud.clawback_deducted(db, withdrawn_key)
        if restored:
            crud.grant_credits(
                db,
                user,
                restored,
                kind="reinstate",
                external_id=f"{dispute_id}:reinstated",
            )
    else:
        crud.clawback_credits(
            db,
            user,
            purchase.amount,
            external_id=withdrawn_key,
            endpoint="stripe:dispute",
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
        session = billing.create_checkout_session(
            user, config.packs[pack], config, str(request.base_url)
        )
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
            # stripe's typed objects raise KeyError (not .get) on missing keys
            try:
                metadata = session["metadata"]
                user_id = int(metadata["gringotts_user_id"])
                credits = int(metadata["credits"])
                session_id = session["id"]
            except (KeyError, TypeError, ValueError):
                logger.error(
                    "gringotts webhook %s: missing/invalid metadata; "
                    "payment cannot be credited",
                    event["id"],
                )
                return {"received": True}
            if credits <= 0:
                # Our own checkout only ever sets a positive pack size, so a
                # non-positive value is a corrupt/forged event: log and ack
                # (do not let it reach grant_credits and 500 into a retry storm).
                logger.error(
                    "gringotts webhook %s: non-positive credits=%s in metadata; "
                    "ignoring",
                    event["id"],
                    credits,
                )
                return {"received": True}
            try:
                payment_status = session["payment_status"]
            except (KeyError, TypeError):
                payment_status = None
            if payment_status not in _PAID_STATUSES:
                # Not settled yet; wait for async_payment_succeeded.
                logger.info(
                    "gringotts webhook %s: payment_status=%s, deferring credit",
                    event["id"],
                    payment_status,
                )
                return {"received": True}
            try:
                amount_cents = int(session["amount_total"])
            except (KeyError, TypeError, ValueError):
                amount_cents = None
            try:
                payment_intent_id = session["payment_intent"]
            except (KeyError, TypeError):
                payment_intent_id = None
            user = crud.get_user(db, user_id)
            if user is None:
                # Usually transient (the user existed when checkout was created;
                # replica lag or a restore in flight). Return non-2xx so Stripe
                # retries — after its retry window a genuine deletion surfaces in
                # the dashboard instead of silently dropping the payment.
                logger.error(
                    "gringotts webhook %s: user %s not found; asking Stripe to retry",
                    event["id"],
                    user_id,
                )
                raise HTTPException(
                    status_code=503, detail="User not found; retry later"
                )
            # Databases created before 0.2.0 keyed purchases on the Stripe
            # event id. If this event was already credited under that scheme,
            # skip it — otherwise re-keying on the session id would double-credit
            # a payment when Stripe retries across the upgrade boundary.
            if crud.external_id_exists(db, event["id"]):
                logger.info(
                    "gringotts webhook: event %s already credited (legacy key)",
                    event["id"],
                )
                return {"received": True}
            # Idempotency is keyed on the checkout session, not the event id:
            # Stripe may emit several event objects for one session.
            granted = crud.grant_credits(
                db,
                user,
                credits,
                kind="purchase",
                external_id=session_id,
                amount_cents=amount_cents,
                payment_intent_id=payment_intent_id,
            )
            if not granted:
                logger.info(
                    "gringotts webhook: checkout session %s already credited",
                    session_id,
                )
        elif event["type"] in _REFUND_EVENTS:
            _process_refund(db, event)
        elif event["type"] == _DISPUTE_WITHDRAWN:
            _process_dispute(db, event, reinstate=False)
        elif event["type"] == _DISPUTE_REINSTATED:
            _process_dispute(db, event, reinstate=True)
        return {"received": True}

    return router
