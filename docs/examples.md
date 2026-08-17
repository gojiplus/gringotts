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

Pass an `Idempotency-Key` header and a retried request applies **exactly once**:
the first request runs and its response is stored; the retry returns that stored
response without re-running the handler — so the charge happens once and the retry
gets the original result back (with an `Idempotent-Replayed: true` header):

```bash
# both calls together charge once; the second returns the first's response
curl -X POST localhost:8000/predict \
  -H "X-API-Key: gk_..." -H "Idempotency-Key: order-42"
curl -X POST localhost:8000/predict \
  -H "X-API-Key: gk_..." -H "Idempotency-Key: order-42"
```

Keys are scoped to the caller, so one caller can't replay another's key. Reusing a
key for a materially different request (method, path, any header, or body) returns
`409`. This includes authorization and dynamic-pricing headers, preventing a replay
from crossing application principals or operation inputs. A
raised error whose debit was refunded releases the key, so a genuine retry can
re-attempt. A handler that returns a `5xx` leaves its debit committed, so that
response is cached. Responses marked `no-store` or too large to retain replay a
marker instead of their original body. The same protection covers the admin grant
route and every other mutating endpoint your app serves.

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
