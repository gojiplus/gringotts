# Examples

## Per-unit (metered) pricing

Compute the cost from the request instead of a fixed number:

```python
from fastapi import Depends, Request
from gringotts import CreditedUser, charge


def cost_from_rows(request: Request) -> int:
    return int(request.headers.get("X-Rows", "1"))


@app.post("/bulk")
def bulk(user: CreditedUser = Depends(charge(cost_from_rows))):
    return {"credits_left": user.credits}
```

A negative or non-integer cost is rejected with `400` — a request can never mint
credits.

## Granting credits in code

`grant` (an alias for the ledger-safe credit function) adds credits with a ledger
row. Pass a unique `external_id` to make event-driven crediting idempotent:

```python
from gringotts import grant
from gringotts.db import SessionLocal

with SessionLocal() as db:
    user = ...  # look up your user
    grant(db, user, 100, kind="promo", external_id="signup-bonus-42")
```

## Idempotent requests

Pass an `Idempotency-Key` header and a retried request is applied at most once —
the repeat returns the first result instead of charging again:

```bash
# both calls together charge once
curl -X POST localhost:8000/predict \
  -H "X-API-Key: gk_..." -H "Idempotency-Key: order-42"
curl -X POST localhost:8000/predict \
  -H "X-API-Key: gk_..." -H "Idempotency-Key: order-42"
```

The same works for the admin grant route and, from the CLI, `gringotts
add-credits <user> <n> --idempotency-key <key>`.

## Admin API

Any user with the admin flag can manage users and credits over HTTP (JSON for
scripts, an HTML dashboard for browsers):

```bash
gringotts create-user ops --admin

curl localhost:8000/gringotts/admin/stats -H "X-API-Key: gk_<admin>"
curl -X POST localhost:8000/gringotts/admin/users/3/grant \
  -H "X-API-Key: gk_<admin>" -d "amount=50"
```

## Operations from the CLI

```bash
gringotts add-credits alice 100
gringotts balance alice
gringotts reconcile   # flag any balance that disagrees with the ledger
gringotts migrate     # apply pending schema changes to an existing database
```

## Seeded demo

```bash
python examples/seed_demo.py    # 5 users + two weeks of simulated traffic
uvicorn examples.demo_app:app
```

The script prints each API key once; open `/gringotts/admin` with the admin key.
