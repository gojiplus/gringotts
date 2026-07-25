import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CreditPack:
    credits: int
    price_cents: int
    name: str
    currency: str = "usd"


@dataclass
class GringottsConfig:
    packs: list[CreditPack] = field(default_factory=list)
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    success_url: str | None = None
    cancel_url: str | None = None
    mount_path: str = "/gringotts"

    def __post_init__(self) -> None:
        self.stripe_secret_key = self.stripe_secret_key or os.getenv("STRIPE_SECRET_KEY")
        self.stripe_webhook_secret = self.stripe_webhook_secret or os.getenv(
            "STRIPE_WEBHOOK_SECRET"
        )

    @property
    def stripe_enabled(self) -> bool:
        return bool(self.packs and self.stripe_secret_key)
