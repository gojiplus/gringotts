from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import gringotts
from gringotts import CreditedUser, GringottsConfig, auth, charge, crud, models

HX = {"HX-Request": "true"}


def make_app():
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    @app.get("/hello")
    def hello(user: CreditedUser = Depends(charge(2))):
        return {"msg": "world"}

    return app


def make_admin(db_session, credits=0):
    return auth.create_user_with_key(db_session, "root", credits=credits, is_admin=True)


def test_usage_endpoint_paginates_newest_first(db_session):
    _, key = auth.create_user_with_key(db_session, "ada", credits=10)
    client = TestClient(make_app())
    client.get("/hello", headers={"X-API-Key": key})
    client.get("/hello", headers={"X-API-Key": key})

    res = client.get("/gringotts/usage", headers={"X-API-Key": key})
    assert res.status_code == 200
    body = res.json()
    assert body["balance"] == 6
    kinds = [t["kind"] for t in body["transactions"]]
    assert kinds == ["charge", "charge", "grant"]

    page = client.get(
        "/gringotts/usage", params={"limit": 1, "offset": 1}, headers={"X-API-Key": key}
    ).json()
    assert len(page["transactions"]) == 1
    assert page["transactions"][0]["kind"] == "charge"

    assert client.get("/gringotts/usage").status_code == 401


def test_admin_routes_reject_non_admins(db_session):
    _, key = auth.create_user_with_key(db_session, "ada", credits=1)
    client = TestClient(make_app())
    assert client.get("/gringotts/admin/stats").status_code == 401
    assert (
        client.get("/gringotts/admin/stats", headers={"X-API-Key": key}).status_code
        == 403
    )
    assert (
        client.get("/gringotts/admin/users", headers={"X-API-Key": key}).status_code
        == 403
    )


def test_admin_replay_conflicts_after_privilege_revocation(db_session):
    admin, key = make_admin(db_session)
    client = TestClient(make_app())
    headers = {"X-API-Key": key, "Idempotency-Key": "admin-list"}

    first = client.get("/gringotts/admin/users", headers=headers)
    crud.set_admin(db_session, admin, False)
    replay = client.get("/gringotts/admin/users", headers=headers)

    assert first.status_code == 200
    assert replay.status_code == 409
    assert replay.headers.get("idempotent-replayed") is None


def test_admin_users_list_and_stats(db_session):
    _, admin_key = make_admin(db_session)
    user, key = auth.create_user_with_key(db_session, "ada", credits=10)
    client = TestClient(make_app())
    client.get("/hello", headers={"X-API-Key": key})
    crud.grant_credits(
        db_session,
        user,
        100,
        kind="purchase",
        external_id="evt_a",
        amount_cents=500,
        currency="usd",
    )

    users = client.get(
        "/gringotts/admin/users", headers={"X-API-Key": admin_key}
    ).json()["users"]
    ada = next(u for u in users if u["username"] == "ada")
    assert ada["balance"] == 108
    assert ada["consumed"] == 2
    assert ada["last_activity"] is not None

    stats = client.get(
        "/gringotts/admin/stats", headers={"X-API-Key": admin_key}
    ).json()
    assert stats["users"] == 2
    assert stats["credits_outstanding"] == 108
    assert stats["credits_consumed"] == 2
    assert stats["credits_purchased"] == 100
    assert stats["revenue_by_currency"] == {"usd": 500}


def test_admin_create_user_key_works_immediately(db_session):
    _, admin_key = make_admin(db_session)
    client = TestClient(make_app())

    res = client.post(
        "/gringotts/admin/users",
        data={"username": "grace", "credits": "5"},
        headers={"X-API-Key": admin_key},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["api_key"].startswith("gk_")
    assert body["credits"] == 5
    assert body["is_admin"] is False

    assert (
        client.get("/hello", headers={"X-API-Key": body["api_key"]}).status_code == 200
    )

    dupe = client.post(
        "/gringotts/admin/users",
        data={"username": "grace"},
        headers={"X-API-Key": admin_key},
    )
    assert dupe.status_code == 409


def test_admin_grant(db_session):
    _, admin_key = make_admin(db_session)
    user, _ = auth.create_user_with_key(db_session, "ada", credits=1)
    client = TestClient(make_app())

    res = client.post(
        f"/gringotts/admin/users/{user.id}/grant",
        data={"amount": "9"},
        headers={"X-API-Key": admin_key},
    )
    assert res.status_code == 200
    assert res.json()["balance"] == 10
    grants = db_session.query(models.CreditTransaction).filter_by(kind="grant").count()
    assert grants == 2  # initial credit + admin grant

    missing = client.post(
        "/gringotts/admin/users/999/grant",
        data={"amount": "1"},
        headers={"X-API-Key": admin_key},
    )
    assert missing.status_code == 404


def test_content_negotiation_html_for_htmx(db_session):
    _, admin_key = make_admin(db_session)
    client = TestClient(make_app())

    as_json = client.get("/gringotts/admin/users", headers={"X-API-Key": admin_key})
    assert as_json.headers["content-type"].startswith("application/json")

    as_html = client.get(
        "/gringotts/admin/users", headers={"X-API-Key": admin_key, **HX}
    )
    assert as_html.headers["content-type"].startswith("text/html")
    assert "<table>" in as_html.text

    stats_html = client.get(
        "/gringotts/admin/stats", headers={"X-API-Key": admin_key, **HX}
    )
    assert 'class="tiles"' in stats_html.text


def test_admin_activity_and_user_usage(db_session):
    _, admin_key = make_admin(db_session)
    user, key = auth.create_user_with_key(db_session, "ada", credits=5)
    client = TestClient(make_app())
    client.get("/hello", headers={"X-API-Key": key})

    activity = client.get(
        "/gringotts/admin/activity", headers={"X-API-Key": admin_key}
    ).json()["transactions"]
    assert activity[0]["kind"] == "charge"
    assert activity[0]["endpoint"] == "/hello"

    usage = client.get(
        f"/gringotts/admin/users/{user.id}/usage", headers={"X-API-Key": admin_key}
    ).json()
    assert usage["username"] == "ada"
    assert usage["balance"] == 3


def test_shell_pages_serve_htmx(db_session):
    client = TestClient(make_app())

    account = client.get("/gringotts/account")
    assert account.status_code == 200
    assert "htmx.min.js" in account.text

    admin = client.get("/gringotts/admin")
    assert admin.status_code == 200
    assert "htmx.min.js" in admin.text

    js = client.get("/gringotts/static/htmx.min.js")
    assert js.status_code == 200
    assert js.headers["content-type"].startswith("text/javascript")


def test_account_panel_fragment(db_session):
    _, key = auth.create_user_with_key(db_session, "ada", credits=7)
    client = TestClient(make_app())
    res = client.get("/gringotts/account/panel", headers={"X-API-Key": key})
    assert res.status_code == 200
    assert "credits remaining" in res.text
    assert client.get("/gringotts/account/panel").status_code == 401
