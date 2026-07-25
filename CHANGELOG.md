# Changelog

All notable changes to this project are documented in this file. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
