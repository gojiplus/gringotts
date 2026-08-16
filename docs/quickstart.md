# Quickstart

Charge for a FastAPI endpoint in a few minutes.

## 1. Install

```bash
pip install gringotts-api
```

The package installs as `gringotts` (the bare PyPI name was taken).

## 2. Create the database and a user

Gringotts uses `DATABASE_URL` (default: a local SQLite file).

```bash
gringotts init-db
gringotts create-user alice --credits 5
# API key (shown once — save it now): gk_...
```

## 3. Guard an endpoint

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
    return {"result": "...", "credits_left": user.credits}
```

Callers send their key as `X-API-Key`; each request costs credits; when a handler
raises, the charge is automatically refunded.

## 4. Call it

```bash
curl -X POST http://localhost:8000/predict -H "X-API-Key: gk_..."
```

When a key runs out, the request returns `402 Payment Required` with a
machine-readable body pointing at the purchase page.

## 5. Sell credits

Add Stripe keys and packs, then point a webhook at `/gringotts/webhook`. See
[Stripe & webhooks](stripe.md) for the events to register and how refunds and
disputes claw credits back.

## Next steps

- [How it works](how-it-works.md) — the ledger, running balance, reconcile, migrate.
- [Examples](examples.md) — per-unit pricing, the admin API, the CLI.
- [API reference](api.md) — the full public surface.
