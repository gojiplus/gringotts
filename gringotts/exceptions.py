from typing import Any

from fastapi import HTTPException


class InvalidAPIKeyError(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=401, detail="Invalid API key")


class PaymentRequiredError(HTTPException):
    """Raised when a key has too few credits.

    `init_app` registers a handler that renders the machine-readable 402 body;
    without it, FastAPI's default HTTPException handler still returns a 402
    with the plain-text detail.
    """

    def __init__(self, cost: int, balance: int) -> None:
        self.cost = cost
        self.balance = balance
        super().__init__(
            status_code=402,
            detail=f"Insufficient credits: request costs {cost}, balance is {balance}",
        )


def payment_required_body(exc: PaymentRequiredError, purchase_url: str | None) -> dict[str, Any]:
    """The frozen 402 response shape (x402/OpenRouter-compatible vocabulary)."""
    accepts: list[dict[str, str]] = []
    if purchase_url:
        accepts.append({"type": "stripe-checkout", "url": purchase_url})
    return {
        "error": {
            "code": "insufficient_credits",
            "type": "payment_required",
            "message": exc.detail,
        },
        "x402Version": 1,
        "cost": exc.cost,
        "balance": exc.balance,
        "accepts": accepts,
    }
