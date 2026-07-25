# Contributing

Development uses [uv](https://docs.astral.sh/uv/):

```bash
uv sync --all-groups
uv run pytest
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pydoclint src/
```

Conformance with the fleet standard is checked by
[preen](https://github.com/gojiplus/preen):

```bash
uvx preen check --strict
```

To run the test suite against Postgres instead of SQLite:

```bash
GRINGOTTS_TEST_DATABASE_URL=postgresql://... uv run pytest
```

## Verifying the Stripe loop end to end

The test suite covers the webhook with locally-signed payloads. To exercise the
real thing — Stripe's hosted Checkout page and Stripe's own event delivery —
put test-mode credentials in a `.env` file (gitignored) and run:

```bash
stripe listen --api-key "$STRIPE_SECRET_KEY" \
  --forward-to localhost:8000/gringotts/webhook
```

Copy the `whsec_...` it prints into `.env` as `STRIPE_WEBHOOK_SECRET`, start
`uvicorn examples.demo_app:app`, then buy a pack from `/gringotts/buy` using
card `4242 4242 4242 4242` with any future expiry and any CVC. You should see
`checkout.session.completed` forwarded with a 200, and the buyer's balance
increase by the pack size.

Two things worth confirming while you are there, because both are easy to
regress:

- Stripe delivers several events per payment (`charge.succeeded`,
  `payment_intent.succeeded`, `charge.updated`, ...). Only
  `checkout.session.completed` should move credits; the rest must return 200
  and do nothing.
- `stripe events resend <event_id>` must not double-credit. That is the same
  path Stripe uses when it retries a delivery, so it is the real test of the
  ledger's `external_id` uniqueness guard.

Releases are tag-driven (`preen release X.Y.Z`); never hand-edit versions —
the git tag is the version.
