# Changelog

## 0.1.0 (unreleased)

First real release. Gringotts is now a packaged library (`pip install
gringotts-api`, imports as `gringotts`) under the MIT license.

### Added

- `charge(cost)` FastAPI dependency: authenticates `X-API-Key`, atomically
  deducts credits, injects the user, and refunds automatically if the endpoint
  raises. `cost` can be an int or a callable computing the cost from the
  request.
- `init_app(app, GringottsConfig(...))`: registers the 402 handler and mounts
  `/gringotts/balance`, `/gringotts/buy`, `/gringotts/checkout`, and
  `/gringotts/webhook`.
- Typed, machine-readable 402 response (x402-compatible vocabulary) with an
  `accepts` array pointing at the purchase page.
- Stripe Checkout integration: declare `CreditPack`s in code; the webhook
  verifies signatures and credits buyers idempotently (unique Stripe event id
  in the ledger).
- `credit_transactions` append-only ledger recording every charge, refund,
  grant, and purchase in the same transaction as the balance update.
- API keys with `gk_` prefix, SHA-256-hashed at rest, last-4 stored for
  display, shown exactly once at creation.
- CLI: `gringotts init-db | create-user [--admin] | set-admin | add-credits |
  balance`.
- `GET /gringotts/usage`: paginated usage history (JSON) for the calling key,
  plus an htmx-powered account page at `/gringotts/account`.
- Admin dashboard at `/gringotts/admin` (stat tiles, users table with inline
  create/grant, live activity feed) and a matching JSON admin API
  (`/admin/stats`, `/admin/users`, `/admin/users/{id}/grant`,
  `/admin/users/{id}/usage`, `/admin/activity`). Admin routes require a user
  with the admin flag; the same routes serve JSON to curl and HTML fragments
  to htmx (vendored, no CDN).
- Purchase ledger rows record the amount paid (`amount_cents` from Stripe's
  `amount_total`), so the dashboard can report revenue.
- `examples/seed_demo.py`: one command seeds users and two weeks of simulated
  traffic for a clickable demo.
- Tooling: hatchling packaging, ruff, mypy, pytest; CI matrix (Python
  3.10–3.13) plus a Postgres job including a parallel-writer no-overspend test.

### Changed

- `@requires_credits(cost)` is now a legacy shim over the new core (it also
  refunds on failure); prefer `Depends(charge(cost))`.

### Removed

- The unmodified Stripe Next.js sample frontend (the library now serves its
  own purchase page), the legacy Express `server.js`, Deta metadata, and the
  drifted `sample_api.yaml`.
- The `api_calls` table — superseded by the `credit_transactions` ledger.
