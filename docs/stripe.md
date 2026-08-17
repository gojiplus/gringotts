# Stripe & webhooks

Gringotts sells credits through **your** Stripe account with Stripe Checkout, and
keeps balances correct as payments settle, get refunded, or are disputed.

## Configuration

Set two secrets (directly on `GringottsConfig` or via the environment):

```bash
export STRIPE_SECRET_KEY=sk_live_...
export STRIPE_WEBHOOK_SECRET=whsec_...
```

Define your packs in code — no Stripe Dashboard product setup is needed:

```python
GringottsConfig(
    packs=[
        CreditPack(credits=100, price_cents=500, name="Starter"),
        CreditPack(credits=1000, price_cents=4000, name="Pro"),
    ],
    stripe_secret_key="sk_live_...",
    stripe_webhook_secret="whsec_...",
)
```

All packs must share one currency.

## Webhook events to register

Point a Stripe webhook at `POST {mount_path}/webhook` and subscribe to:

| Event | Why |
|---|---|
| `checkout.session.completed` | Grant credits once a payment is `paid`. |
| `checkout.session.async_payment_succeeded` | Delayed methods (e.g. ACH) settle after the completed event — credit only then. |
| `refund.created`, `refund.updated` | Claw back a proportional share of the granted credits when a payment is refunded. |
| `charge.dispute.funds_withdrawn` | Claw back the purchase when a dispute pulls funds. |
| `charge.dispute.funds_reinstated` | Re-credit if the dispute is later resolved in your favor. |

Every event is signature-verified against `STRIPE_WEBHOOK_SECRET`; an invalid
signature is rejected with `400`.

## Crediting

Credits are granted only once the checkout session's `payment_status` is `paid`
(or `no_payment_required`). Before redirecting the buyer, Gringotts persists the
authorized user, credits, amount, and currency in `checkout_orders`. Fulfillment
requires the signed Session to match that local order exactly; unrelated Checkout
integrations on the same Stripe account are ignored. Idempotency is keyed on the
**checkout session id**, so Stripe re-delivering an event—or sending more than one
event for the same session—never double-credits.

## Clawback (refunds and disputes)

When a payment is reversed, gringotts reverses the credits it granted:

- **Refunds** claw back proportionally: a refund of `R` of a payment of `C` that
  granted `N` credits reverses `round(N × R ⁄ C)`. Multiple partial refunds are
  tracked cumulatively, so rounding never over- or under-claws.
- **Disputes** claw back on `funds_withdrawn` and re-credit on `funds_reinstated`.
  Inquiry (`warning_*`) events move no money and are ignored.
- Clawback is **clamped at zero** — it never drives a balance negative, deducting
  only the credits the user still holds. When it can't fully recover, it logs a
  warning so you can follow up.
- It's idempotent on the Stripe `Refund`/`Dispute` id, and correlates events to
  the original purchase by the **PaymentIntent** recorded at Checkout time.

## Zero-decimal currencies

`price_cents` is the amount in the currency's *smallest unit*. For zero-decimal
currencies (JPY, KRW, and others), that unit is the whole currency — `price_cents=500`
means ¥500, not ¥5.00 — and gringotts displays it accordingly.
Purchase, refund, dispute, and reinstatement rows retain the ISO currency, so
historical revenue is totaled separately per currency.

## Local testing

```bash
stripe listen --forward-to localhost:8000/gringotts/webhook
```

## Upgrading

Run `gringotts migrate` after upgrading. Drain Checkout Sessions created by 0.3.x
before moving to 0.4.0; the new release fulfills only Sessions backed by a local
`checkout_orders` row. Two older caveats remain:

- Purchases recorded by **0.1.x** were keyed on the Stripe event id, not the
  checkout session. Drain any in-flight delayed payments before upgrading so a
  settlement arriving afterward isn't credited twice.
- Clawback correlates only purchases made on **0.3.0+** (older rows have no stored
  PaymentIntent); a refund or dispute on a pre-0.3 purchase is logged, not
  auto-clawed.
