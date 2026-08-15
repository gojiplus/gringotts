"""Library configuration passed to `init_app`."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CreditPack:
    """A purchasable bundle: `credits` for `price_cents` in `currency`."""

    credits: int
    price_cents: int
    name: str
    currency: str = "usd"

    def __post_init__(self) -> None:
        """Reject packs that would charge for nothing or subtract credits."""
        if self.credits <= 0:
            raise ValueError("CreditPack.credits must be positive")
        if self.price_cents < 0:
            raise ValueError("CreditPack.price_cents cannot be negative")


@dataclass
class GringottsConfig:
    """Settings for the mounted routes and Stripe integration.

    Stripe keys fall back to the `STRIPE_SECRET_KEY` and
    `STRIPE_WEBHOOK_SECRET` environment variables when not set explicitly.
    """

    packs: list[CreditPack] = field(default_factory=list)
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    success_url: str | None = None
    cancel_url: str | None = None
    mount_path: str = "/gringotts"

    def __post_init__(self) -> None:
        """Fill Stripe credentials from the environment when not provided."""
        self.stripe_secret_key = self.stripe_secret_key or os.getenv(
            "STRIPE_SECRET_KEY"
        )
        self.stripe_webhook_secret = self.stripe_webhook_secret or os.getenv(
            "STRIPE_WEBHOOK_SECRET"
        )

    @property
    def stripe_enabled(self) -> bool:
        """Whether credit purchases are possible (packs plus a Stripe key)."""
        return bool(self.packs and self.stripe_secret_key)
