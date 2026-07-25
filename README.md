# Gringotts

**Prepaid credits for your FastAPI, with your own Stripe account, in 5 minutes.**

You built a useful API. Gringotts lets you charge for it per request — API
keys, atomic credit deduction, a purchase page, and a Stripe webhook that
actually credits the buyer — all inside your own app and database. No SaaS
metering service, no gateway in front of your API, no revenue share.

```python
from fastapi import Depends, FastAPI

import gringotts
from gringotts import CreditedUser, CreditPack, GringottsConfig, charge

app = FastAPI()

gringotts.init_app(
    app,
    GringottsConfig(packs=[CreditPack(credits=100, price_cents=500, name="Starter")]),
)


@app.post("/predict")
def predict(user: CreditedUser = Depends(charge(1))):
    return {"result": "..."}
```

That's the whole integration. Callers send `X-API-Key`; each request costs
credits; when they run out they get a machine-readable HTTP 402 pointing at
your purchase page; Stripe Checkout tops them up.

## Why gringotts

- **Self-hosted, zero SaaS**: your database, your Stripe account. No per-request
  network calls, no rev share, no vendor that can shut down under you.
- **Correct where it's hard to be**: atomic credit deduction (no overspend under
  concurrency), automatic refund when your handler raises, idempotent webhook
  crediting, and an append-only ledger auditing every credit movement.
- **Agent-ready 402**: the insufficient-credits response is typed JSON
  (x402-compatible vocabulary), so AI-agent clients can parse it and pay.

## Install

```bash
pip install gringotts-api
```

The package installs as `gringotts` (the bare PyPI name was taken).

## Quickstart

1. **Create the database and a user** (uses `DATABASE_URL`, default
   `sqlite:///./gringotts.db`):

   ```bash
   gringotts init-db
   gringotts create-user alice --credits 5
   # API key (shown once — save it now): gk_...
   ```

2. **Guard your endpoints** with `charge(cost)` as in the example above, or
   compute the cost from the request:

   ```python
   cost_per_row = lambda request: int(request.headers["X-Rows"])


   @app.post("/big-job")
   def big_job(user: CreditedUser = Depends(charge(cost_per_row))):
       return {"status": "queued"}
   ```

3. **Call it**:

   ```bash
   curl -X POST http://localhost:8000/predict -H "X-API-Key: gk_..."
   ```

4. **Sell credits.** Set two environment variables and define your packs:

   ```bash
   export STRIPE_SECRET_KEY=sk_live_...
   export STRIPE_WEBHOOK_SECRET=whsec_...
   ```

   `init_app` mounts, under `/gringotts`:

   | Route | What it does |
   |---|---|
   | `GET /gringotts/buy` | Minimal purchase page listing your packs |
   | `POST /gringotts/checkout` | Redirects the buyer to Stripe Checkout |
   | `POST /gringotts/webhook` | Verifies the Stripe signature and credits the buyer — exactly once, even if Stripe retries |
   | `GET /gringotts/balance` | Balance for the calling `X-API-Key` |
   | `GET /gringotts/usage` | Paginated usage history (JSON) for the calling key |
   | `GET /gringotts/account` | Account page for your users: balance, usage, buy link |
   | `GET /gringotts/admin` | Admin dashboard (requires an admin key, see below) |

   Point a Stripe webhook (event `checkout.session.completed`) at
   `POST /gringotts/webhook`. For local testing:

   ```bash
   stripe listen --forward-to localhost:8000/gringotts/webhook
   ```

## Try it in 2 minutes (seeded demo)

```bash
python examples/seed_demo.py        # creates 5 users + 2 weeks of fake traffic
uvicorn examples.demo_app:app
```

The seed script prints every API key once. Then:

- Open `http://localhost:8000/gringotts/admin`, paste the **admin** key — stat
  tiles (users, credits outstanding/consumed/purchased, revenue), a users table
  with inline create-user and grant-credits forms, and an activity feed that
  refreshes every 5 seconds.
- `curl -X POST localhost:8000/predict -H "X-API-Key: <ada's key>" -d '{"text":"hi"}' -H "Content-Type: application/json"`
  a few times and watch the feed update.
- Open `http://localhost:8000/gringotts/account`, paste ada's key — balance,
  recent usage, and the buy link.

## Admin dashboard and API

Users with the admin flag (`gringotts create-user ops --admin`, or
`gringotts set-admin alice`) can use the dashboard at `/gringotts/admin` and
the JSON API with their own `gk_` key. Every admin route returns JSON for
plain clients and an HTML fragment for the dashboard (htmx is vendored —
no CDN, no build step):

```bash
curl localhost:8000/gringotts/admin/stats -H "X-API-Key: gk_<admin>"
curl localhost:8000/gringotts/admin/users -H "X-API-Key: gk_<admin>"
curl -X POST localhost:8000/gringotts/admin/users -H "X-API-Key: gk_<admin>" \
  -d "username=carol&credits=10"          # returns the new key, once
curl -X POST localhost:8000/gringotts/admin/users/3/grant \
  -H "X-API-Key: gk_<admin>" -d "amount=50"
curl localhost:8000/gringotts/admin/users/3/usage -H "X-API-Key: gk_<admin>"
curl localhost:8000/gringotts/admin/activity -H "X-API-Key: gk_<admin>"
```

The dashboard and account pages keep the pasted key in `sessionStorage` and
send it as a header on every request — serve them over HTTPS in production.

## The 402 response

When a key has too few credits, gringotts returns `402 Payment Required` with
a frozen, machine-readable body:

```json
{
  "error": {
    "code": "insufficient_credits",
    "type": "payment_required",
    "message": "Insufficient credits: request costs 5, balance is 2"
  },
  "x402Version": 1,
  "cost": 5,
  "balance": 2,
  "accepts": [
    {"type": "stripe-checkout", "url": "https://api.example.com/gringotts/buy"}
  ]
}
```

`accepts` lists the ways a caller (human or agent) can pay; today that's your
Stripe Checkout purchase page. Additional schemes (e.g. Stripe's Machine
Payments Protocol) can be added later without breaking the shape.

## How it stores things

Two tables, created by `gringotts init-db`:

- `users` — username, SHA-256 hash of the API key (the key itself is shown
  once and never stored), last 4 characters for display, current balance.
- `credit_transactions` — an append-only ledger. Every charge, refund, grant,
  and purchase is a signed row written in the same transaction as the balance
  update, so the ledger always sums to the balance. Purchases carry the Stripe
  event id under a unique constraint — that's what makes webhook crediting
  idempotent.

Works on SQLite out of the box and Postgres via
`DATABASE_URL=postgresql://...` (both run in CI, including a parallel-writer
test that proves no overspend).

## Configuration

| Setting | Where | Default |
|---|---|---|
| `DATABASE_URL` | env | `sqlite:///./gringotts.db` |
| `STRIPE_SECRET_KEY` | env or `GringottsConfig(stripe_secret_key=...)` | — |
| `STRIPE_WEBHOOK_SECRET` | env or `GringottsConfig(stripe_webhook_secret=...)` | — |
| `packs` | `GringottsConfig(packs=[CreditPack(...)])` | `[]` |
| `success_url` / `cancel_url` | `GringottsConfig` | back to the purchase page |
| `mount_path` | `GringottsConfig` | `/gringotts` |

## CLI

```bash
gringotts init-db
gringotts create-user alice --credits 5
gringotts create-user ops --admin
gringotts set-admin alice          # or --revoke
gringotts add-credits alice 100
gringotts balance alice
```

## Not yet (deliberately)

Subscriptions and recurring billing, postpaid invoicing, decimal or
multi-currency pricing, rate limiting, x402/MPP crypto settlement,
key rotation, and schema migrations (pre-1.0, recreate with `init-db`). The
ledger is designed so these can be added without schema breaks.

Known v0.1 limitation: a charge is refunded when your handler *raises*; a
handler that *returns* a 5xx response, or a process crash mid-request, is not
auto-refunded — both are visible in the ledger.

## Development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
mypy gringotts
```

## License

MIT
