"""Typed errors and the machine-readable 402 body."""

from typing import Any

from fastapi import HTTPException


class InvalidAPIKeyError(HTTPException):
    """Raised when the X-API-Key header is missing or matches no user (401)."""

    def __init__(self) -> None:
        """Build the fixed 401 response."""
        super().__init__(status_code=401, detail="Invalid API key")


class PaymentRequiredError(HTTPException):
    """Raised when a key has too few credits.

    `init_app` registers a handler that renders the machine-readable 402 body;
    without it, FastAPI's default HTTPException handler still returns a 402
    with the plain-text detail.
    """

    def __init__(self, cost: int, balance: int) -> None:
        """Record the attempted cost and current balance for the 402 body."""
        self.cost = cost
        self.balance = balance
        super().__init__(
            status_code=402,
            detail=f"Insufficient credits: request costs {cost}, balance is {balance}",
        )


def payment_required_body(
    exc: PaymentRequiredError, purchase_url: str | None
) -> dict[str, Any]:
    """Return the frozen 402 response shape (x402/OpenRouter-compatible)."""
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
