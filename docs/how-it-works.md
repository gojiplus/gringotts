# How it works

Gringotts keeps credits correct with a small, auditable data model and a few
invariants it never breaks.

## The ledger and the balance

Three tables hold everything:

- **`users`** — one row per API consumer: the SHA-256 hash of their key (the key
  itself is shown once and never stored), the last four characters for display,
  and the cached balance `credits`.
- **`credit_transactions`** — an append-only ledger. Every charge, refund, grant,
  purchase, clawback, and reinstatement is one signed row (`amount` is negative
  for charges and clawbacks, positive otherwise), written **in the same database
  transaction** as the balance update.
- **`idempotency_records`** — the response cache and lock for keyed requests,
  uniquely indexed by caller and `Idempotency-Key`.

So the cached `credits` is a fast read, but the ledger is the source of truth:
the sum of a user's `amount` values always equals their balance.

## Running balance and three-way reconcile

Each ledger row also stores **`balance_after`** — the user's balance immediately
after that row. That makes the balance auditable at every point in history, and
it makes drift *structurally* detectable rather than merely possible to notice.

`gringotts reconcile` (and the admin path) run a **three-way check** per user:

> cached `credits` == running `SUM(amount)` == the latest row's `balance_after`,
> and every earlier row's `balance_after` equals its cumulative sum.

If any of those disagree, the user is reported. In normal operation they never
do — every write goes through one transaction that updates the balance and
appends the row together.

## Never negative

A charge is a single compare-and-set — `UPDATE ... SET credits = credits - :cost
WHERE credits >= :cost` — so two concurrent requests can't overspend, with no
row locks needed on PostgreSQL. Two database `CHECK` constraints back the
invariant up: `credits >= 0` on `users` and `balance_after >= 0` on the ledger.

## Concurrency

Charges use the compare-and-set above. Refund, grant, and clawback take a
per-user write lock first (a real `UPDATE` that acquires a row lock on Postgres
and the database write lock on SQLite) so their read-modify-write — including the
cumulative refund math — is serialized. On SQLite the engine also ships WAL and a
`busy_timeout` so concurrent writers wait rather than erroring.

## Migrations

Schema changes ship as forward-only, idempotent steps applied in place by
`gringotts migrate` — no "drop and recreate." It refuses to run if the ledger
does not already reconcile, so it never backfills on top of drift. `init-db`
creates a fresh schema already at the latest version.

## The 402 response

When a key has too few credits, gringotts returns `402 Payment Required` with a
frozen, machine-readable body (x402-compatible vocabulary) carrying the `cost`,
`balance`, and an `accepts` array pointing at the purchase page — so an AI-agent
client can parse it and pay. The exact shape is documented in the README.
