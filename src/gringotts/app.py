"""Application wiring: `init_app` registers the 402 handler and mounts routes."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.routing import Match

from .admin import build_admin_router
from .config import GringottsConfig
from .exceptions import PaymentRequiredError, payment_required_body
from .idempotency import IdempotencyMiddleware
from .router import build_router


def init_app(app: FastAPI, config: GringottsConfig | None = None) -> None:
    """Register the 402 handler and mount the gringotts routes.

    Routes (balance, usage, account, purchase page, checkout, webhook, admin)
    land under ``config.mount_path``.

    Args:
        app: The FastAPI application to wire gringotts into.
        config: Settings; a default ``GringottsConfig`` (no packs, Stripe off)
            is used when omitted.

    Example:
        >>> from fastapi import FastAPI
        >>> import gringotts
        >>> from gringotts import GringottsConfig, CreditPack
        >>> app = FastAPI()
        >>> gringotts.init_app(
        ...     app,
        ...     GringottsConfig(
        ...         packs=[CreditPack(credits=100, price_cents=500, name="Starter")]
        ...     ),
        ... )
    """
    cfg = config or GringottsConfig()
    app.state.gringotts = cfg

    async def handle_payment_required(request: Request, exc: PaymentRequiredError):
        purchase_url = None
        if cfg.stripe_enabled:
            base = str(request.base_url).rstrip("/")
            purchase_url = f"{base}{cfg.mount_path}/buy"
        return JSONResponse(
            status_code=402, content=payment_required_body(exc, purchase_url)
        )

    app.add_exception_handler(PaymentRequiredError, handle_payment_required)  # type: ignore[arg-type]
    builtin_route_ids: set[int] = set()

    before = len(app.router.routes)
    app.include_router(build_router(cfg), prefix=cfg.mount_path, tags=["gringotts"])
    builtin_route_ids.update(id(route) for route in app.router.routes[before:])
    before = len(app.router.routes)
    app.include_router(
        build_admin_router(cfg), prefix=cfg.mount_path, tags=["gringotts-admin"]
    )
    builtin_route_ids.update(id(route) for route in app.router.routes[before:])

    def is_builtin_route(scope: dict) -> bool:
        """Return whether Starlette's first matching route belongs to Gringotts."""
        for route in app.router.routes:
            match, _ = route.matches(scope)
            if match is Match.FULL:
                return id(route) in builtin_route_ids
        return False

    if cfg.idempotency_enabled:
        # Wraps the whole app: an Idempotency-Key request short-circuits to its
        # stored response on replay, so the handler (and its charge) runs once.
        app.add_middleware(
            IdempotencyMiddleware,
            header=cfg.idempotency_header,
            max_key_length=cfg.idempotency_max_key_length,
            max_body_bytes=cfg.idempotency_max_body_bytes,
            max_response_bytes=cfg.idempotency_max_response_bytes,
            retention_seconds=cfg.idempotency_retention_seconds,
            form_api_key_path=f"{cfg.mount_path}/checkout",
            builtin_route_validator=is_builtin_route,
            replay_validator=cfg.idempotency_replay_validator,
        )
