"""Admin HTTP API + htmx dashboard.

Every data route answers JSON for plain clients (curl, scripts) and an HTML
fragment when called by htmx (`HX-Request` header) — one route, two faces.
All routes require an API key whose user has `is_admin`.
"""

import html

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy.orm import Session

from . import auth, crud, pages
from .config import GringottsConfig, format_money
from .db import get_session
from .dependencies import require_admin
from .models import User


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def build_admin_router(config: GringottsConfig) -> APIRouter:
    """Build the admin router (dashboard shell, stats, users, activity)."""
    router = APIRouter()
    mount = config.mount_path

    @router.get("/admin", response_class=HTMLResponse)
    def dashboard():
        body = f"""
<div id="stats" hx-get="{mount}/admin/stats" hx-trigger="load, every 10s">
  <p class="muted">Paste an admin API key above.</p>
</div>
<div class="grid2">
  <section>
    <h2>Users</h2>
    <div id="users" hx-get="{mount}/admin/users"
         hx-trigger="load, users-changed from:body"></div>
    <details>
      <summary>Create user</summary>
      <form hx-post="{mount}/admin/users" hx-target="#create-result" class="inline">
        <input name="username" placeholder="username" required>
        <input name="credits" type="number" value="0" min="0">
        <label><input name="is_admin" type="checkbox" value="true"> admin</label>
        <button type="submit">Create</button>
      </form>
      <div id="create-result"></div>
    </details>
  </section>
  <section>
    <h2>Activity</h2>
    <div id="activity" hx-get="{mount}/admin/activity"
         hx-trigger="load, every 5s"></div>
  </section>
</div>"""
        return pages.shell("Gringotts admin", body, mount)

    @router.get("/admin/stats")
    def stats(
        request: Request,
        admin: User = Depends(require_admin),
        db: Session = Depends(get_session),
    ):
        data = crud.aggregate_stats(db)
        if not _is_htmx(request):
            return JSONResponse(data)
        currency = config.packs[0].currency if config.packs else "usd"
        tiles = (
            pages.tile(str(data["users"]), "users")
            + pages.tile(str(data["credits_outstanding"]), "credits outstanding")
            + pages.tile(str(data["credits_consumed"]), "credits consumed")
            + pages.tile(str(data["credits_purchased"]), "credits purchased")
            + pages.tile(format_money(data["revenue_cents"], currency), "revenue")
        )
        return HTMLResponse(f'<div class="tiles">{tiles}</div>')

    @router.get("/admin/users")
    def users(
        request: Request,
        admin: User = Depends(require_admin),
        db: Session = Depends(get_session),
    ):
        data = crud.list_users_with_stats(db)
        if not _is_htmx(request):
            return JSONResponse({"users": data})
        rows = "".join(
            f"<tr><td>{html.escape(u['username'])}{' ★' if u['is_admin'] else ''}</td>"
            f"<td>...{html.escape(u['key_last4'])}</td>"
            f'<td class="num">{u["balance"]}</td>'
            f'<td class="num">{u["consumed"]}</td>'
            f"<td>{html.escape((u['last_activity'] or '—')[:16])}</td>"
            f"<td><form class='inline' hx-post='{mount}/admin/users/{u['id']}/grant'"
            f" hx-target='#users'>"
            f"<input name='amount' type='number' value='10' min='1'>"
            f"<button type='submit'>Grant</button></form></td></tr>"
            for u in data
        )
        return HTMLResponse(
            "<table><thead><tr><th>User</th><th>Key</th>"
            '<th class="num">Balance</th><th class="num">Consumed</th>'
            "<th>Last activity</th><th></th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )

    @router.post("/admin/users")
    def create_user(
        request: Request,
        username: str = Form(...),
        credits: int = Form(0),
        is_admin: bool = Form(False),
        admin: User = Depends(require_admin),
        db: Session = Depends(get_session),
    ):
        if credits < 0:
            raise HTTPException(
                status_code=400, detail="Initial credits cannot be negative"
            )
        if crud.get_user_by_username(db, username) is not None:
            raise HTTPException(status_code=409, detail="Username already exists")
        user, api_key = auth.create_user_with_key(
            db, username, credits, is_admin=is_admin
        )
        # The response carries the one-time API key; no-store keeps the idempotency
        # middleware from persisting that secret in its response cache.
        no_store = {"Cache-Control": "no-store"}
        if not _is_htmx(request):
            return JSONResponse(
                {
                    "id": user.id,
                    "username": user.username,
                    "credits": user.credits,
                    "is_admin": user.is_admin,
                    "api_key": api_key,
                },
                status_code=201,
                headers=no_store,
            )
        fragment = (
            f"<p>Created <strong>{html.escape(user.username)}</strong>."
            " API key (shown once):</p>"
            f'<div class="keyreveal">{html.escape(api_key)}</div>'
        )
        return HTMLResponse(
            fragment, headers={"HX-Trigger": "users-changed", **no_store}
        )

    @router.post("/admin/users/{user_id}/grant")
    def grant(
        request: Request,
        user_id: int,
        amount: int = Form(...),
        admin: User = Depends(require_admin),
        db: Session = Depends(get_session),
    ):
        if amount <= 0:
            raise HTTPException(status_code=400, detail="Grant amount must be positive")
        user = crud.get_user(db, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        # Retried grants are made safe by IdempotencyMiddleware: a repeat with the
        # same Idempotency-Key returns this response without re-running the grant.
        crud.grant_credits(db, user, amount)
        db.refresh(user)
        if not _is_htmx(request):
            return JSONResponse({"id": user.id, "balance": user.credits})
        return users(request, admin, db)

    @router.get("/admin/users/{user_id}/usage")
    def user_usage(
        user_id: int,
        limit: int = 50,
        offset: int = 0,
        admin: User = Depends(require_admin),
        db: Session = Depends(get_session),
    ):
        user = crud.get_user(db, user_id)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        transactions = crud.list_transactions(
            db, user_id=user_id, limit=max(1, min(limit, 500)), offset=max(0, offset)
        )
        return {
            "username": user.username,
            "balance": user.credits,
            "transactions": [
                {
                    "amount": t.amount,
                    "kind": t.kind,
                    "endpoint": t.endpoint,
                    "amount_cents": t.amount_cents,
                    "created_at": t.created_at.isoformat(),
                }
                for t in transactions
            ],
        }

    @router.get("/admin/activity")
    def activity(
        request: Request,
        limit: int = 25,
        admin: User = Depends(require_admin),
        db: Session = Depends(get_session),
    ) -> Response:
        transactions = crud.list_transactions(db, limit=max(1, min(limit, 500)))
        if not _is_htmx(request):
            return JSONResponse(
                {
                    "transactions": [
                        {
                            "user_id": t.user_id,
                            "amount": t.amount,
                            "kind": t.kind,
                            "endpoint": t.endpoint,
                            "created_at": t.created_at.isoformat(),
                        }
                        for t in transactions
                    ]
                }
            )
        usernames = {u.id: u.username for u in db.query(User).all()}
        rows = "".join(
            f"<tr><td>{t.created_at:%m-%d %H:%M}</td>"
            f"<td>{html.escape(usernames.get(t.user_id, '?'))}</td>"
            f"<td>{html.escape(t.kind)}</td>"
            f"<td>{html.escape(t.endpoint or '—')}</td>"
            f'<td class="num">{t.amount:+d}</td></tr>'
            for t in transactions
        )
        return HTMLResponse(
            "<table><thead><tr><th>When</th><th>User</th><th>Kind</th>"
            '<th>Endpoint</th><th class="num">Credits</th></tr></thead>'
            f"<tbody>{rows}</tbody></table>"
        )

    return router
