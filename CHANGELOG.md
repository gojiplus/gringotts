# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-08-17

### Added

- **Response-caching idempotency.** Send an `Idempotency-Key` header with any
  mutating request (a `charge()`-guarded endpoint, the admin grant route, anything
  the app serves) and a retry applies **exactly once**: the first request runs and
  its response is stored; a later request with the same key from the same caller
  gets that stored response back **without re-running the handler** — so the charge,
  and any other side effect in the handler, happens once. Because the
  replay short-circuits above the route, a retry can never double-charge, re-run the
  handler for free, or race the original's refund.
  - Keys are **scoped to the caller** (the API key), so one caller can't replay
    another's key.
  - Reusing a key for a materially different request (method, path, headers, or
    body) returns `409 Conflict`; a replay carries an `Idempotent-Replayed: true`
    header. Binding all headers prevents a changed authorization or pricing header
    from replaying another operation's response.
  - A raised error whose charge was **successfully refunded** releases the key, so
    a genuine retry can re-attempt. A returned response—including a returned
    `5xx`—is cached because its charge remains committed. If compensation fails,
    the key stays locked rather than risking a second debit. A `4xx` (including a
    `402` for insufficient credits) is cached; use a fresh key for a fresh attempt.
  - Only **authenticated callers** create records (an invalid key can't fill the
    table); an oversized request body is rejected with `413` and a response too
    large to cache keeps the key locked with a marker; records **expire** after
    `idempotency_retention_seconds`.
  - A response marked `Cache-Control: no-store` is not persisted — so the admin
    create-user route's one-time API key is never written to the response cache.
  - An **in-flight or crashed** request is never auto-re-run — a duplicate or a
    retry of an unknown outcome gets `409`, because age can't prove the first
    attempt didn't already charge. A reused key expires lazily; to bound table
    growth from keys that are never retried, schedule `gringotts prune-idempotency`
    (e.g. a daily cron).
  - Configurable via `GringottsConfig`: `idempotency_enabled` (default on),
    `idempotency_header`, `idempotency_max_key_length`, `idempotency_max_body_bytes`,
    `idempotency_max_response_bytes`, `idempotency_retention_seconds`.
  - Backed by a new `idempotency_records` table (applied by `gringotts migrate`).
  - Known limitations (deliberate): a replay returns the stored response without
    re-checking authorization (a credential revoked mid-window can replay its own
    prior responses until they expire — lower `idempotency_retention_seconds` to
    shrink the window); only the **charge** is guaranteed exactly-once, so any
    *other* non-idempotent side effect a handler commits before raising is the
    application's responsibility; and a crashed/disconnected in-flight request
    leaks its lock (a `409` for retries, cleared by `prune-idempotency
    --include-in-flight`).
- Curated documentation site: a grouped API reference, documented config fields,
  usage examples, and new guides (Quickstart, How it works, Stripe & webhooks,
  Examples).

### Fixed

- A failed compensating refund no longer releases an idempotency key and permits a
  second debit; when a request has multiple charges, every debit must have an exact
  confirmed refund before the request becomes retryable.
- FastAPI 0.118.0 or newer is required so `charge()` can observe streaming and
  background-task failures and compensate their debits before releasing a key.
- SQLAlchemy 2.0.44 or newer is required for supported Python 3.13 and 3.14
  runtimes; CI now executes the full suite against the runtime and test dependency
  lower bounds.
- Concurrent first attempts are serialized by the caller/key claim, and stale
  requests cannot mutate a newer claim after emergency in-flight pruning and
  primary-key reuse.
- Handler-generated trailing-slash redirects retain and replay their key even when
  no credit charge occurred; only FastAPI's pre-route automatic redirect releases
  the claim for the redirected request.
- Pruning an expired completed record concurrently with lazy key reclamation no
  longer produces a spurious in-progress conflict.
- Proportional Stripe clawbacks use exact integer rounding rather than floats, so
  large credit balances cannot be over- or under-clawed through precision loss, and
  dispute reinstatements preserve intervening refunds regardless of event order.
- Incomplete immutable Stripe webhook snapshots now retrieve their current Checkout
  Session, Refund, or Dispute before accounting instead of dropping a payment or
  retrying an event whose contents cannot change.
- Credit movements and pack prices reject fractional, string, and Boolean quantities
  at runtime so SQLite, dynamic pricing, and Checkout configuration cannot admit
  non-integers into the ledger or payment flow.
- Paid Checkout, refund, and dispute events with incomplete settlement data return a
  retryable error instead of granting or clawing credits from an unverifiable
  event snapshot. When an immutable refund event omits its settlement status,
  Gringotts retrieves the current Refund from Stripe instead of retrying that same
  incomplete snapshot forever.
- Stripe fulfillment is bound to a locally persisted Checkout order. A signed event
  from another Checkout integration—or one whose user, credits, amount, or currency
  differs from the authorized order—cannot mint credits.
- Purchase and reversal rows store their ISO currency, and revenue is aggregated by
  currency instead of silently adding incompatible minor units.

### Removed

- The legacy `gringotts.decorators.requires_credits` API was removed because a
  decorator cannot observe failures that occur while streaming a response or
  running a background task. Use `Depends(charge(cost))`, which compensates the
  debit across the full response lifecycle.

### Upgrading

- Run `gringotts migrate` to create the `idempotency_records` and
  `checkout_orders` tables and add the ledger currency column. Historical monetary
  rows have unknown currency; new revenue is reported separately by ISO currency.
- Drain Checkout Sessions created by 0.3.x before upgrading. Version 0.4.0 fulfills
  only Sessions tied to a locally persisted `checkout_orders` row.

## [0.3.0] - 2026-08-15

Correct money accounting: refunds and disputes now reverse the credits they
granted, currency display is fixed for zero-decimal currencies, and consumption
stats net out refunds.

### Added

- **Refund and dispute clawback.** The webhook now handles `refund.created` /
  `refund.updated` (claws back a proportional share of the granted credits —
  `round(credits * refund_amount / amount_paid)`) and
  `charge.dispute.funds_withdrawn` (claws back the purchase), and re-credits on
  `charge.dispute.funds_reinstated`. Clawback is **clamped at zero** (never drives
  a balance negative), idempotent on the Stripe `Refund`/`Dispute` id, and logs a
  warning when it can't fully recover. Register the new events on your Stripe
  webhook.
- **PaymentIntent correlation.** Purchase rows store `payment_intent_id`, and
  Checkout sets `payment_intent_data.metadata`, so refund/dispute events (which
  carry the PaymentIntent, not the checkout session) map back to the purchase
  with no extra API call.
- **Zero-decimal currency support.** `config.is_zero_decimal` /
  `config.format_money`; the buy page and admin revenue tile now show whole units
  for JPY/KRW/etc. instead of dividing by 100. `price_cents` is documented as the
  amount in the currency's smallest unit.

### Changed

- Admin `credits_consumed` (dashboard and JSON) is now **net of refunds** — a
  fully refunded request counts as zero consumption.

### Upgrading

- Run `gringotts migrate` to add `payment_intent_id` to an existing database.
- Clawback correlates only purchases made on 0.3.0+ (older purchase rows have no
  stored PaymentIntent); a refund/dispute on a pre-0.3 purchase is logged and
  acknowledged, not auto-clawed.

## [0.2.0] - 2026-08-15

A money-correctness pass (an independent multi-model audit) plus a
strong-consistency and in-place-upgrade foundation.

### Fixed

- **Negative/zero cost can no longer mint credits.** `charge()` rejects a
  negative client-derived cost (e.g. `X-Units: -1000`, which previously
  *increased* the balance) with a 400, and a zero cost is a no-op with no
  ledger row.
- **Webhook checks `payment_status` before crediting** and now also handles
  `checkout.session.async_payment_succeeded`, so a delayed (ACH) payment credits
  only once it settles — not on the initial unpaid `completed` event.
- **Webhook idempotency is keyed on the Checkout Session id**, not the Stripe
  event id, so two events for one paid session no longer double-credit the buyer.
- **`charge()` uses its own database session** instead of the host app's request
  session: it no longer commits the caller's uncommitted work, loses the
  compensating refund when the handler leaves its session in a failed
  transaction, or lets a failing handler's mutations to the user (e.g.
  `is_admin = True`) get committed by the auto-refund.
- **Non-positive amounts are rejected server-side** on admin grant, CLI
  `add-credits`, admin create-user initial credits, and `CreditPack` (the HTML
  `min` attribute was the only guard before).
- **Unfulfillable paid webhooks are no longer silently acknowledged**:
  unknown-user returns 503 so Stripe retries (a usually-transient case), while
  malformed or non-positive metadata is logged and acknowledged.
- **`grant_credits` only swallows a duplicate `external_id`** as "already
  processed"; any other integrity error re-raises instead of silently dropping a
  valid grant.

### Added

- **`balance_after` running-balance** on every ledger row, written in the same
  transaction, with a database `CHECK (balance_after >= 0)`. `gringotts
  reconcile` is now a three-way check that a user's cached `credits`, the running
  `SUM(amount)`, and the latest `balance_after` all agree.
- **`gringotts migrate`** — forward-only, idempotent, in-place schema upgrades
  (no recreate); refuses to run if the ledger doesn't already reconcile. And
  **`gringotts reconcile`** to report any balance/ledger disagreement.
- **`CHECK (credits >= 0)`** on the `users` table as a database-level backstop.
- SQLite engine now uses **WAL and a `busy_timeout`** (default 30s,
  `GRINGOTTS_SQLITE_BUSY_TIMEOUT`) so concurrent writers wait rather than
  erroring with "database is locked."

### Changed

- `charge()` no longer participates in the host application's database
  transaction — it owns its own session for the charge and any refund.
- Docs corrected to the actual toolchain (`pyright`, `uv sync`) and updated for
  the webhook events, `balance_after`, and `gringotts migrate`.

### Upgrading

- Run `gringotts migrate` to bring an existing database to the new schema (adds
  `balance_after`); it refuses to run if the ledger doesn't already reconcile.
- Webhook idempotency now keys on the checkout-session id. Purchases recorded by
  0.1.x were keyed on the Stripe *event* id and can't be de-duplicated against a
  later settlement event. **Before upgrading, drain any in-flight delayed (ACH)
  payments** — a settlement that arrives after the upgrade could otherwise be
  credited twice. `gringotts migrate` warns when it finds pre-0.2 purchase rows.

## [0.1.0] - 2026-07-25

First real release. Gringotts is now a packaged library (`pip install
gringotts-api`, imports as `gringotts`) under the MIT license.

### Added

- `charge(cost)` FastAPI dependency: authenticates `X-API-Key`, atomically
  deducts credits, injects the user, and refunds automatically if the endpoint
  raises. `cost` can be an int or a callable computing the cost from the
  request.
- `init_app(app, GringottsConfig(...))`: registers the 402 handler and mounts
  the balance, usage, account, purchase, webhook, and admin routes.
- Typed, machine-readable 402 response (x402-compatible vocabulary) with an
  `accepts` array pointing at the purchase page.
- Stripe Checkout integration: declare `CreditPack`s in code; the webhook
  verifies signatures and credits buyers idempotently (unique Stripe event id
  in the ledger). Purchase rows record the amount paid for revenue reporting.
- `credit_transactions` append-only ledger recording every charge, refund,
  grant, and purchase in the same transaction as the balance update.
- Admin dashboard at `/gringotts/admin` (stat tiles, users table with inline
  create/grant, live activity feed) and a matching JSON admin API; the same
  routes serve JSON to plain clients and HTML fragments to htmx (vendored).
- `GET /gringotts/usage` and an account page at `/gringotts/account` so API
  consumers can monitor their balance and history.
- API keys with `gk_` prefix, SHA-256-hashed at rest, last-4 stored for
  display, shown exactly once at creation.
- CLI: `gringotts init-db | create-user [--admin] | set-admin | add-credits |
  balance`.
- `examples/seed_demo.py`: one command seeds users and two weeks of simulated
  traffic for a clickable demo.
- Fleet-standard tooling (py-canon): uv, ruff, pyright, pydoclint, src layout,
  tag-driven versioning, reusable CI/docs/release workflows, PyPI trusted
  publishing with attestations.

### Changed

- `@requires_credits(cost)` is now a legacy shim over the new core (it also
  refunds on failure); prefer `Depends(charge(cost))`.

### Removed

- The unmodified Stripe Next.js sample frontend (the library serves its own
  pages), the legacy Express `server.js`, Deta metadata, and the drifted
  `sample_api.yaml`.
- The `api_calls` table — superseded by the `credit_transactions` ledger.
