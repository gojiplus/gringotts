"""Library configuration passed to `init_app`."""

import os
from collections.abc import Callable
from dataclasses import dataclass, field

# Currencies with no minor unit — Stripe's amount is already the whole-unit value
# (e.g. JPY 500 means ¥500, not ¥5.00). Source of truth:
# https://docs.stripe.com/currencies#zero-decimal
_ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "bif",
        "clp",
        "djf",
        "gnf",
        "jpy",
        "kmf",
        "krw",
        "mga",
        "pyg",
        "rwf",
        "ugx",
        "vnd",
        "vuv",
        "xaf",
        "xof",
        "xpf",
    }
)


def is_zero_decimal(currency: str) -> bool:
    """Whether `currency` has no minor unit (so `price_cents` is whole units)."""
    return currency.lower() in _ZERO_DECIMAL_CURRENCIES


def format_money(minor_units: int, currency: str) -> str:
    """Format a Stripe smallest-unit amount for display, currency-aware."""
    upper = currency.upper()
    if is_zero_decimal(currency):
        return f"{minor_units:,} {upper}"
    return f"{minor_units / 100:,.2f} {upper}"


@dataclass(frozen=True)
class CreditPack:
    """A purchasable bundle of credits sold through Stripe Checkout.

    Attributes:
        credits: How many credits the buyer receives. Must be positive.
        price_cents: The price in the currency's smallest unit — cents for USD,
            whole yen for JPY and other zero-decimal currencies.
        name: Human-readable pack name shown on the buy page and in Checkout.
        currency: ISO currency code (default ``"usd"``). All packs in one
            ``GringottsConfig`` must share a currency.
    """

    credits: int
    price_cents: int
    name: str
    currency: str = "usd"

    def __post_init__(self) -> None:
        """Reject packs that would charge for nothing or subtract credits."""
        if isinstance(self.credits, bool) or not isinstance(self.credits, int):
            raise TypeError("CreditPack.credits must be an integer")
        if isinstance(self.price_cents, bool) or not isinstance(self.price_cents, int):
            raise TypeError("CreditPack.price_cents must be an integer")
        if self.credits <= 0:
            raise ValueError("CreditPack.credits must be positive")
        if self.price_cents < 0:
            raise ValueError("CreditPack.price_cents cannot be negative")
        if (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isalpha()
        ):
            raise ValueError("CreditPack.currency must be a three-letter ISO code")
        object.__setattr__(self, "currency", self.currency.lower())


@dataclass
class GringottsConfig:
    """Settings for the mounted routes and Stripe integration.

    Stripe keys fall back to the ``STRIPE_SECRET_KEY`` and
    ``STRIPE_WEBHOOK_SECRET`` environment variables when not set explicitly.

    Attributes:
        packs: Credit packs offered for sale. Empty disables purchasing; all
            packs must share one currency.
        stripe_secret_key: Stripe secret key (or ``STRIPE_SECRET_KEY`` env).
            Required, with packs, to enable Checkout.
        stripe_webhook_secret: Stripe webhook signing secret (or
            ``STRIPE_WEBHOOK_SECRET`` env). Required to accept webhooks.
        success_url: Where Checkout returns on success; defaults to the buy page
            with ``?status=success``.
        cancel_url: Where Checkout returns on cancel; defaults to the buy page
            with ``?status=cancelled``.
        mount_path: URL prefix the routes mount under (default ``/gringotts``).
        idempotency_enabled: Whether to install the response-caching idempotency
            middleware so an ``Idempotency-Key`` header makes a request safe to
            retry (default ``True``).
        idempotency_header: The header carrying the idempotency key (default
            ``"Idempotency-Key"``).
        idempotency_max_key_length: Reject a key longer than this with ``400``
            (default ``255``).
        idempotency_max_body_bytes: A keyed request whose body exceeds this is
            rejected with ``413`` before the app runs (default ``1_000_000``).
        idempotency_max_response_bytes: A response larger than this is streamed to
            the client but replays a marker rather than the body (default
            ``1_000_000``).
        idempotency_retention_seconds: A stored record older than this is treated
            as expired — a reused key re-runs, bounding table growth and key
            lifetime (default ``86_400``).
        idempotency_replay_validator: Synchronous callback receiving the ASGI scope
            before a host-application response is replayed. Return ``True`` only
            after revalidating any mutable host authorization. Without one, host
            retries stay safe but return ``409`` instead of cached response data.
    """

    packs: list[CreditPack] = field(default_factory=list)
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    success_url: str | None = None
    cancel_url: str | None = None
    mount_path: str = "/gringotts"
    idempotency_enabled: bool = True
    idempotency_header: str = "Idempotency-Key"
    idempotency_max_key_length: int = 255
    idempotency_max_body_bytes: int = 1_000_000
    idempotency_max_response_bytes: int = 1_000_000
    idempotency_retention_seconds: float = 86_400.0
    idempotency_replay_validator: Callable[[dict], bool] | None = None

    def __post_init__(self) -> None:
        """Fill Stripe credentials from env and require one currency across packs."""
        self.stripe_secret_key = self.stripe_secret_key or os.getenv(
            "STRIPE_SECRET_KEY"
        )
        self.stripe_webhook_secret = self.stripe_webhook_secret or os.getenv(
            "STRIPE_WEBHOOK_SECRET"
        )
        # A single Checkout page offers one currency; historical revenue remains
        # grouped by the currency stored on each ledger row.
        currencies = {pack.currency.lower() for pack in self.packs}
        if len(currencies) > 1:
            raise ValueError(
                f"all credit packs must use one currency, got {sorted(currencies)}"
            )

    @property
    def stripe_enabled(self) -> bool:
        """Whether credit purchases are possible (packs plus a Stripe key)."""
        return bool(self.packs and self.stripe_secret_key)
