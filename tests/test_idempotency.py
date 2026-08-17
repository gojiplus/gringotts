"""Response-caching idempotency: a retried request applies at most once.

The mechanism is :class:`gringotts.idempotency.IdempotencyMiddleware`: the first
request with an ``Idempotency-Key`` runs and its response is stored; a later
request with the same key from the same caller gets that stored response back
without re-running the handler.
"""

import asyncio
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi import Depends, FastAPI, Response
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from test_charge import make_app

import gringotts
from gringotts import CreditedUser, GringottsConfig, auth, charge, crud, models
from gringotts import db as gdb
from gringotts.db import Base, make_engine
from gringotts.idempotency import IdempotencyMiddleware, _fingerprint


def test_replay_returns_cached_response_and_charges_once(db_session):
    user, key = auth.create_user_with_key(db_session, "a", credits=10)
    client = TestClient(make_app())
    h = {"X-API-Key": key, "Idempotency-Key": "req-1"}
    r1 = client.get("/hello", headers=h)
    r2 = client.get("/hello", headers=h)  # retry, same key
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json() == r1.json()  # the stored response, byte-for-byte
    assert r1.json()["credits"] == 8  # first charge left 8
    assert r2.headers.get("idempotent-replayed") == "true"
    db_session.refresh(user)
    assert user.credits == 8  # charged once, not twice
    assert (
        db_session.query(models.CreditTransaction).filter_by(kind="charge").count() == 1
    )


def test_distinct_keys_each_charge(db_session):
    user, key = auth.create_user_with_key(db_session, "b", credits=10)
    client = TestClient(make_app())
    client.get("/hello", headers={"X-API-Key": key, "Idempotency-Key": "k1"})
    client.get("/hello", headers={"X-API-Key": key, "Idempotency-Key": "k2"})
    db_session.refresh(user)
    assert user.credits == 6  # two distinct charges of 2


def test_without_key_each_charges(db_session):
    user, key = auth.create_user_with_key(db_session, "c", credits=10)
    client = TestClient(make_app())
    client.get("/hello", headers={"X-API-Key": key})
    client.get("/hello", headers={"X-API-Key": key})
    db_session.refresh(user)
    assert user.credits == 6  # no key -> each request charges


def test_key_scoped_per_caller(db_session):
    # a shared key must not let caller B replay caller A's cached response
    ua, ka = auth.create_user_with_key(db_session, "ua", credits=10)
    ub, kb = auth.create_user_with_key(db_session, "ub", credits=10)
    client = TestClient(make_app())
    client.get("/hello", headers={"X-API-Key": ka, "Idempotency-Key": "shared"})
    client.get("/hello", headers={"X-API-Key": kb, "Idempotency-Key": "shared"})
    db_session.refresh(ua)
    db_session.refresh(ub)
    assert ua.credits == 8
    assert ub.credits == 8  # B was charged, not handed A's cached free response


def test_conflict_on_different_request(db_session):
    user, key = auth.create_user_with_key(db_session, "cf", credits=10)
    client = TestClient(make_app())
    assert (
        client.get(
            "/hello", headers={"X-API-Key": key, "Idempotency-Key": "k"}
        ).status_code
        == 200
    )
    # same key, different path (method+path+query+body fingerprint differs) -> 409
    r = client.get(
        "/units", headers={"X-API-Key": key, "Idempotency-Key": "k", "X-Units": "1"}
    )
    assert r.status_code == 409
    db_session.refresh(user)
    assert user.credits == 8  # only /hello (cost 2) applied


def test_key_pins_the_operation(db_session):
    # headers aren't part of the fingerprint: the key pins the whole operation to
    # the first request, so a same-path replay returns the first result verbatim.
    user, key = auth.create_user_with_key(db_session, "kp", credits=10)
    client = TestClient(make_app())
    h = {"X-API-Key": key, "Idempotency-Key": "u"}
    r1 = client.get("/units", headers={**h, "X-Units": "1"})  # cost 1
    r2 = client.get("/units", headers={**h, "X-Units": "9"})  # replay of r1
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json() == r1.json()
    db_session.refresh(user)
    assert user.credits == 9  # only the first (cost 1) charge applied


def test_server_error_is_not_cached_and_retry_reattempts(db_session):
    user, key = auth.create_user_with_key(db_session, "rb", credits=10)
    client = TestClient(make_app(), raise_server_exceptions=False)
    h = {"X-API-Key": key, "Idempotency-Key": "b1"}
    assert client.get("/boom", headers=h).status_code == 500  # charged, raised, refund
    assert client.get("/boom", headers=h).status_code == 500  # re-attempted, not cached
    db_session.refresh(user)
    assert user.credits == 10  # each attempt charged then refunded -> net zero
    assert (
        db_session.query(models.CreditTransaction).filter_by(kind="charge").count() == 2
    )


def test_retry_after_failure_charges_once(db_session):
    # first keyed attempt fails (500, refunded, not cached); the retry that
    # succeeds is charged fresh — not replayed into free successful work.
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())
    calls = {"n": 0}

    @app.get("/flaky")
    def flaky(user: CreditedUser = Depends(charge(2))):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient failure")
        return {"ok": True}

    user, key = auth.create_user_with_key(db_session, "fk", credits=10)
    client = TestClient(app, raise_server_exceptions=False)
    h = {"X-API-Key": key, "Idempotency-Key": "flk"}
    assert client.get("/flaky", headers=h).status_code == 500  # fails, refunded
    assert client.get("/flaky", headers=h).status_code == 200  # succeeds, charged
    db_session.refresh(user)
    assert user.credits == 8  # paid once for the successful call, not free
    assert crud.find_balance_discrepancies(db_session) == []


def test_insufficient_credits_is_cached(db_session):
    _, key = auth.create_user_with_key(db_session, "cr", credits=1)
    client = TestClient(make_app())
    h = {"X-API-Key": key, "Idempotency-Key": "p"}
    r1 = client.get("/hello", headers=h)  # cost 2 > balance 1 -> 402
    r2 = client.get("/hello", headers=h)
    assert r1.status_code == 402
    assert r2.status_code == 402  # a 4xx is a deterministic result -> cached
    assert r2.headers.get("idempotent-replayed") == "true"
    assert r2.json() == r1.json()


def test_admin_grant_idempotent_via_middleware(db_session):
    from test_router import make_app as make_stripe_app

    _, admin_key = auth.create_user_with_key(
        db_session, "root", credits=0, is_admin=True
    )
    victim, _ = auth.create_user_with_key(db_session, "v", credits=0)
    client = TestClient(make_stripe_app())
    h = {"X-API-Key": admin_key, "Idempotency-Key": "admin-grant-1"}
    r1 = client.post(
        f"/gringotts/admin/users/{victim.id}/grant", data={"amount": "25"}, headers=h
    )
    r2 = client.post(
        f"/gringotts/admin/users/{victim.id}/grant", data={"amount": "25"}, headers=h
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.headers.get("idempotent-replayed") == "true"
    db_session.refresh(victim)
    assert victim.credits == 25  # granted once despite two identical requests


def test_oversized_key_is_rejected(db_session):
    _, key = auth.create_user_with_key(db_session, "big", credits=10)
    client = TestClient(make_app())
    h = {"X-API-Key": key, "Idempotency-Key": "x" * 300}
    assert client.get("/hello", headers=h).status_code == 400


def test_idempotency_can_be_disabled(db_session):
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig(idempotency_enabled=False))

    @app.get("/hello")
    def hello(user: CreditedUser = Depends(charge(2))):
        return {"credits": user.credits}

    user, key = auth.create_user_with_key(db_session, "off", credits=10)
    client = TestClient(app)
    h = {"X-API-Key": key, "Idempotency-Key": "k"}
    client.get("/hello", headers=h)
    client.get("/hello", headers=h)
    db_session.refresh(user)
    assert user.credits == 6  # both charged; middleware absent


def test_in_progress_request_conflicts(db_session):
    # a fresh in-flight record for the same (caller, key) holds off a duplicate
    user, key = auth.create_user_with_key(db_session, "ip", credits=10)
    db_session.add(
        models.IdempotencyRecord(
            api_key_hash=auth.get_api_key_hash(key),
            idempotency_key="ipk",
            request_fingerprint=_fingerprint("GET", "/hello", b"", b""),
            completed=False,
        )
    )
    db_session.commit()
    client = TestClient(make_app())
    r = client.get("/hello", headers={"X-API-Key": key, "Idempotency-Key": "ipk"})
    assert r.status_code == 409
    db_session.refresh(user)
    assert user.credits == 10  # not charged while the first is in flight


def test_concurrent_first_attempts_run_handler_once(tmp_path, monkeypatch):
    url = os.getenv("GRINGOTTS_TEST_DATABASE_URL") or (
        f"sqlite:///{tmp_path}/idempotency-race.db"
    )
    engine = make_engine(url)
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(gdb, "SessionLocal", session_local)

    with session_local() as setup:
        user, key = auth.create_user_with_key(setup, "race", credits=10)
        user_id = user.id

    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())
    calls = 0
    calls_lock = threading.Lock()

    @app.get("/once")
    def once(user: CreditedUser = Depends(charge(2))):
        nonlocal calls
        with calls_lock:
            calls += 1
        return {"credits": user.credits}

    # Make both requests finish caller validation before either can claim the
    # unique (caller, key) row. This exercises the actual insert race rather than
    # merely sending a duplicate after the first request is already in flight.
    barrier = threading.Barrier(2)
    original = IdempotencyMiddleware._caller_exists

    def caller_exists_together(self, api_key):
        exists = original(self, api_key)
        barrier.wait(timeout=10)
        return exists

    monkeypatch.setattr(IdempotencyMiddleware, "_caller_exists", caller_exists_together)
    headers = {"X-API-Key": key, "Idempotency-Key": "same-first-attempt"}

    def request_once(_):
        return TestClient(app).get("/once", headers=headers)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(request_once, range(2)))
        assert {response.status_code for response in responses} <= {200, 409}
        assert any(response.status_code == 200 for response in responses)
        assert calls == 1
        with session_local() as check:
            assert crud.get_user(check, user_id).credits == 8
            assert (
                check.query(models.CreditTransaction)
                .filter_by(user_id=user_id, kind="charge")
                .count()
                == 1
            )
            assert crud.find_balance_discrepancies(check) == []
    finally:
        engine.dispose()


def test_in_progress_is_never_reclaimed_by_age(db_session):
    # an old in-flight record (owner may have crashed after charging) must NOT be
    # auto-rerun — its outcome is unknown, so a retry stays a 409
    user, key = auth.create_user_with_key(db_session, "st", credits=10)
    db_session.add(
        models.IdempotencyRecord(
            api_key_hash=auth.get_api_key_hash(key),
            idempotency_key="stk",
            request_fingerprint=_fingerprint("GET", "/hello", b"", b""),
            completed=False,
            created_at=datetime.now(UTC) - timedelta(days=2),
        )
    )
    db_session.commit()
    client = TestClient(make_app())
    r = client.get("/hello", headers={"X-API-Key": key, "Idempotency-Key": "stk"})
    assert r.status_code == 409  # ambiguous in-flight op, not re-run
    db_session.refresh(user)
    assert user.credits == 10  # not charged


def test_invalid_key_creates_no_record(db_session):
    # an unauthenticated caller must not be able to fill idempotency_records
    client = TestClient(make_app())
    r = client.get("/hello", headers={"X-API-Key": "gk_bogus", "Idempotency-Key": "z"})
    assert r.status_code == 401
    assert db_session.query(models.IdempotencyRecord).count() == 0


def test_replay_preserves_response_headers(db_session):
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    @app.get("/h")
    def h(response: Response, user: CreditedUser = Depends(charge(1))):
        response.headers["X-Custom"] = "kept"
        return {"ok": True}

    _, key = auth.create_user_with_key(db_session, "hd", credits=10)
    client = TestClient(app)
    hd = {"X-API-Key": key, "Idempotency-Key": "hk"}
    r1 = client.get("/h", headers=hd)
    r2 = client.get("/h", headers=hd)
    assert r1.headers.get("X-Custom") == "kept"
    assert r2.headers.get("X-Custom") == "kept"  # header replayed, not just body
    assert r2.headers.get("idempotent-replayed") == "true"


def test_expired_record_reruns(db_session):
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig(idempotency_retention_seconds=0))

    @app.get("/e")
    def e(user: CreditedUser = Depends(charge(2))):
        return {"c": user.credits}

    user, key = auth.create_user_with_key(db_session, "ex", credits=10)
    client = TestClient(app)
    h = {"X-API-Key": key, "Idempotency-Key": "ek"}
    client.get("/e", headers=h)
    client.get("/e", headers=h)  # prior record already expired -> re-runs
    db_session.refresh(user)
    assert user.credits == 6  # both charged; the cached row expired


def test_response_over_cap_keeps_lock_and_charges_once(db_session):
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig(idempotency_max_response_bytes=10))

    @app.get("/big")
    def big(user: CreditedUser = Depends(charge(2))):
        return {"data": "x" * 100}  # far over the 10-byte cap

    user, key = auth.create_user_with_key(db_session, "bg", credits=10)
    client = TestClient(app)
    h = {"X-API-Key": key, "Idempotency-Key": "bk"}
    r1 = client.get("/big", headers=h)
    assert r1.json()["data"] == "x" * 100  # first request gets the full response
    r2 = client.get("/big", headers=h)  # replay: lock kept, handler NOT re-run
    assert r2.headers.get("idempotent-response-not-cached") == "true"
    db_session.refresh(user)
    assert user.credits == 8  # charged once; the oversized response didn't free the key


def test_body_over_cap_is_rejected_not_run(db_session):
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig(idempotency_max_body_bytes=10))

    ran = {"n": 0}

    @app.post("/up")
    def up(user: CreditedUser = Depends(charge(2))):
        ran["n"] += 1
        return {"ok": True}

    user, key = auth.create_user_with_key(db_session, "up", credits=10)
    client = TestClient(app)
    h = {"X-API-Key": key, "Idempotency-Key": "uk"}
    r = client.post("/up", headers=h, content=b"x" * 100)  # oversized keyed body
    assert r.status_code == 413  # rejected before any side effect
    db_session.refresh(user)
    assert user.credits == 10  # not charged
    assert ran["n"] == 0  # handler never ran
    assert db_session.query(models.IdempotencyRecord).count() == 0


def test_disconnect_mid_upload_runs_nothing(db_session):
    # a truncated (disconnected) keyed request must not run the wrapped app, so it
    # can never charge and then be retried into a double charge
    _, key = auth.create_user_with_key(db_session, "dc", credits=10)
    called = {"v": False}

    async def dummy_app(scope, receive, send):
        called["v"] = True

    mw = IdempotencyMiddleware(dummy_app)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/up",
        "query_string": b"",
        "headers": [(b"x-api-key", key.encode()), (b"idempotency-key", b"dk")],
    }
    messages = iter(
        [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(mw(scope, receive, send))
    assert called["v"] is False  # app never invoked on the aborted upload
    assert sent == []  # nothing sent for a client that's already gone
    assert db_session.query(models.IdempotencyRecord).count() == 0


def test_returned_5xx_is_cached_and_charges_once(db_session):
    # a handler that RETURNS a 5xx after charging committed the charge, so a retry
    # must get the cached 5xx, not run (and charge) again
    from fastapi.responses import JSONResponse

    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())
    ran = {"n": 0}

    @app.get("/ret5xx")
    def ret5xx(user: CreditedUser = Depends(charge(2))):
        ran["n"] += 1
        return JSONResponse({"detail": "downstream failed"}, status_code=503)

    user, key = auth.create_user_with_key(db_session, "r5", credits=10)
    client = TestClient(app)
    h = {"X-API-Key": key, "Idempotency-Key": "r5k"}
    assert client.get("/ret5xx", headers=h).status_code == 503
    r2 = client.get("/ret5xx", headers=h)
    assert r2.status_code == 503  # replayed, not re-run
    assert r2.headers.get("idempotent-replayed") == "true"
    assert ran["n"] == 1  # handler ran once
    db_session.refresh(user)
    assert user.credits == 8  # charged once


def test_read_body_detects_disconnect():
    mw = IdempotencyMiddleware(None)
    messages = iter(
        [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(messages)

    body, state = asyncio.run(mw._read_body(receive))
    assert state == "disconnect"
    assert body == b"partial"


def test_read_body_flags_overflow():
    mw = IdempotencyMiddleware(None, max_body_bytes=4)
    messages = iter(
        [
            {"type": "http.request", "body": b"12345", "more_body": True},
        ]
    )

    async def receive():
        return next(messages)

    body, state = asyncio.run(mw._read_body(receive))
    assert state == "overflow"
    assert body == b""  # oversized body isn't buffered — the request is rejected


def test_background_task_failure_refunds_and_does_not_double_charge(db_session):
    # a handler returns 200 then its background task raises; charge() refunds, so
    # the middleware must NOT cache the (paid) 200 — that would hand a retry the
    # success output for free — and must never double-charge.
    from fastapi.responses import JSONResponse
    from starlette.background import BackgroundTask

    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    def boom():
        raise RuntimeError("background failure")

    @app.get("/bg")
    def bg(user: CreditedUser = Depends(charge(2))):
        return JSONResponse({"ok": True}, background=BackgroundTask(boom))

    user, key = auth.create_user_with_key(db_session, "bgk", credits=10)
    client = TestClient(app, raise_server_exceptions=False)
    h = {"X-API-Key": key, "Idempotency-Key": "bgk1"}
    client.get("/bg", headers=h)  # 200 returned, bg raises -> charge refunded
    client.get("/bg", headers=h)  # key released -> re-runs, refunds again
    db_session.refresh(user)
    assert user.credits == 10  # refunded each time; never double-charged
    assert crud.find_balance_discrepancies(db_session) == []


def test_streaming_response_is_cached_without_hang(db_session):
    # exercises the replay_receive path: a StreamingResponse makes Starlette poll
    # receive for disconnect, so replaying the terminal message forever would hang
    from fastapi.responses import StreamingResponse

    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    @app.get("/stream")
    def stream(user: CreditedUser = Depends(charge(2))):
        def gen():
            yield b"chunk1"
            yield b"chunk2"

        return StreamingResponse(gen(), media_type="text/plain")

    user, key = auth.create_user_with_key(db_session, "sm", credits=10)
    client = TestClient(app)
    h = {"X-API-Key": key, "Idempotency-Key": "sk"}
    r1 = client.get("/stream", headers=h)
    assert r1.content == b"chunk1chunk2"
    r2 = client.get("/stream", headers=h)
    assert r2.content == b"chunk1chunk2"  # replayed intact
    assert r2.headers.get("idempotent-replayed") == "true"
    db_session.refresh(user)
    assert user.credits == 8  # charged once


def test_no_store_response_body_is_not_persisted(db_session):
    # the admin create-user response carries the one-time API key; caching it must
    # not persist that secret in idempotency_records
    from test_router import make_app as make_stripe_app

    _, admin_key = auth.create_user_with_key(
        db_session, "root", credits=0, is_admin=True
    )
    client = TestClient(make_stripe_app())
    h = {"X-API-Key": admin_key, "Idempotency-Key": "mk1"}
    r = client.post("/gringotts/admin/users", data={"username": "newbie"}, headers=h)
    assert r.status_code == 201
    new_key = r.json()["api_key"]
    # the response was cached (key locked) but WITHOUT the secret body
    rec = (
        db_session.query(models.IdempotencyRecord)
        .filter_by(idempotency_key="mk1")
        .one()
    )
    assert rec.completed is True
    assert rec.response_body is not None
    assert new_key.encode() not in rec.response_body  # the API key is not stored
    # a replay returns the marker, not the secret
    r2 = client.post("/gringotts/admin/users", data={"username": "newbie"}, headers=h)
    assert new_key not in r2.text
    assert r2.headers.get("idempotent-response-not-cached") == "true"


def test_interrupted_stream_refunds_and_releases_key(db_session):
    # a streaming handler that raises mid-stream is refunded by charge(); the key
    # is released (the partial body is never cached as a complete response) and no
    # double-charge can occur
    from fastapi.responses import StreamingResponse

    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    @app.get("/partial")
    def partial(user: CreditedUser = Depends(charge(2))):
        def gen():
            yield b"first"
            raise RuntimeError("stream broke")

        return StreamingResponse(gen(), media_type="text/plain")

    user, key = auth.create_user_with_key(db_session, "ps", credits=10)
    client = TestClient(app, raise_server_exceptions=False)
    h = {"X-API-Key": key, "Idempotency-Key": "pk"}
    client.get("/partial", headers=h)  # started, then raised mid-stream -> refunded
    # refunded -> key released, no partial body cached as a complete response
    assert (
        db_session.query(models.IdempotencyRecord)
        .filter_by(idempotency_key="pk")
        .count()
        == 0
    )
    db_session.refresh(user)
    assert user.credits == 10  # refunded; not charged for the broken stream
    assert crud.find_balance_discrepancies(db_session) == []


def test_prune_retains_in_flight_by_default(db_session):
    from datetime import UTC, datetime, timedelta

    old = datetime.now(UTC) - timedelta(days=2)
    db_session.add(
        models.IdempotencyRecord(
            api_key_hash="h",
            idempotency_key="done",
            request_fingerprint="f",
            completed=True,
            created_at=old,
        )
    )
    db_session.add(
        models.IdempotencyRecord(
            api_key_hash="h",
            idempotency_key="inflight",
            request_fingerprint="f",
            completed=False,  # a live lock, even though old
            created_at=old,
        )
    )
    db_session.commit()
    deleted = crud.purge_idempotency_records(db_session, older_than_seconds=86_400)
    assert deleted == 1  # only the completed one; the in-flight lock is retained
    remaining = {r.idempotency_key for r in db_session.query(models.IdempotencyRecord)}
    assert remaining == {"inflight"}
    # explicit opt-in clears the in-flight lock too
    crud.purge_idempotency_records(
        db_session, older_than_seconds=86_400, include_in_flight=True
    )
    assert db_session.query(models.IdempotencyRecord).count() == 0


def test_httpexception_5xx_releases_key(db_session):
    # a handler that raises HTTPException(503) is refunded by charge(); the 5xx is
    # a transient failure, so the key must be released for a retry to re-attempt
    from fastapi import HTTPException

    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())
    calls = {"n": 0}

    @app.get("/svc")
    def svc(user: CreditedUser = Depends(charge(2))):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HTTPException(status_code=503, detail="transient")
        return {"ok": True}

    user, key = auth.create_user_with_key(db_session, "sv", credits=10)
    client = TestClient(app)
    h = {"X-API-Key": key, "Idempotency-Key": "svk"}
    assert client.get("/svc", headers=h).status_code == 503  # refunded, key released
    assert client.get("/svc", headers=h).status_code == 200  # retry re-attempts
    db_session.refresh(user)
    assert user.credits == 8  # charged once, for the successful retry


def test_slash_redirect_does_not_conflict(db_session):
    # FastAPI auto-redirects POST /x -> /x/; the client follows with the same key,
    # and the redirect must not be cached as a conflicting key reuse
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    @app.post("/x/")
    def x(user: CreditedUser = Depends(charge(2))):
        return {"ok": True}

    user, key = auth.create_user_with_key(db_session, "sr", credits=10)
    client = TestClient(app)
    h = {"X-API-Key": key, "Idempotency-Key": "srk"}
    r = client.post("/x", headers=h, follow_redirects=True)
    assert r.status_code == 200  # followed the 307 to /x/, not a 409
    assert r.json() == {"ok": True}
    db_session.refresh(user)
    assert user.credits == 8  # charged once at /x/


def test_endpoint_slash_redirect_after_charge_is_cached(db_session):
    # an endpoint that CHARGES then itself returns a 307 to its own path+slash must
    # NOT be mistaken for a router redirect and released — that would double-charge
    from fastapi.responses import RedirectResponse

    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    @app.get("/y")
    def y(user: CreditedUser = Depends(charge(2))):
        return RedirectResponse("/y/", status_code=307)

    user, key = auth.create_user_with_key(db_session, "yy", credits=10)
    client = TestClient(app)
    h = {"X-API-Key": key, "Idempotency-Key": "yk"}
    r1 = client.get("/y", headers=h, follow_redirects=False)
    assert r1.status_code == 307
    rec = (
        db_session.query(models.IdempotencyRecord).filter_by(idempotency_key="yk").one()
    )
    assert rec.completed is True  # committed charge -> cached, not released
    r2 = client.get("/y", headers=h, follow_redirects=False)
    assert r2.headers.get("idempotent-replayed") == "true"
    db_session.refresh(user)
    assert user.credits == 8  # charged once


def test_refund_failure_keeps_key_locked(db_session, monkeypatch):
    # if compensation itself fails, the charge still stands, so the key must NOT be
    # released — a retry would otherwise charge again on top of the un-refunded debit
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    @app.get("/rf")
    def rf(user: CreditedUser = Depends(charge(2))):
        raise RuntimeError("handler failure")

    user, key = auth.create_user_with_key(db_session, "rf", credits=10)

    def _boom(*args, **kwargs):
        raise RuntimeError("refund failure")

    monkeypatch.setattr(crud, "refund_user", _boom)
    client = TestClient(app, raise_server_exceptions=False)
    h = {"X-API-Key": key, "Idempotency-Key": "rfk"}
    client.get("/rf", headers=h)  # charged, handler fails, refund FAILS
    client.get("/rf", headers=h)  # key not released -> not re-charged
    db_session.refresh(user)
    assert user.credits == 8  # the un-refunded charge stands once, never doubled


def test_old_request_cannot_mutate_reused_record_id(db_session):
    middleware = IdempotencyMiddleware(None)
    outcome, old_claim, _ = middleware._claim("caller-a", "old", "fingerprint-a")
    assert outcome == "new"
    assert old_claim is not None
    old_id, _ = old_claim

    # Simulate the documented emergency cleanup while the original request still
    # exists, then force a new owner to reuse the integer primary key.
    crud.purge_idempotency_records(
        db_session, older_than_seconds=-1, include_in_flight=True
    )
    replacement = models.IdempotencyRecord(
        id=old_id,
        api_key_hash="caller-b",
        idempotency_key="new",
        request_fingerprint="fingerprint-b",
        completed=False,
        created_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    db_session.add(replacement)
    db_session.commit()

    middleware._delete(old_claim)
    middleware._store(old_claim, 200, b"old", "[]")
    db_session.expire_all()
    current = db_session.get(models.IdempotencyRecord, old_id)
    assert current is not None
    assert current.api_key_hash == "caller-b"
    assert current.completed is False
    assert current.response_body is None


def test_fingerprint_no_collision_with_null_bytes():
    # length-prefixing must make the encoding injective: two materially different
    # requests (query/body split differently around a null byte) can't collide
    a = _fingerprint("POST", "/x", b"a\x00", b"")
    b = _fingerprint("POST", "/x", b"a", b"\x00")
    assert a != b


def test_bodyless_status_stores_no_marker_body(db_session):
    app = FastAPI()
    gringotts.init_app(app, GringottsConfig())

    @app.get("/nc")
    def nc(user: CreditedUser = Depends(charge(1))):
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    _, key = auth.create_user_with_key(db_session, "nc", credits=10)
    client = TestClient(app)
    h = {"X-API-Key": key, "Idempotency-Key": "nck"}
    r1 = client.get("/nc", headers=h)
    assert r1.status_code == 204
    rec = (
        db_session.query(models.IdempotencyRecord)
        .filter_by(idempotency_key="nck")
        .one()
    )
    assert rec.response_body == b""  # a 204 must have no body, marker or otherwise
    r2 = client.get("/nc", headers=h)
    assert r2.status_code == 204
    assert r2.content == b""  # replay stays bodyless


def test_prune_idempotency_records(db_session):
    from datetime import UTC, datetime, timedelta

    db_session.add(
        models.IdempotencyRecord(
            api_key_hash="h",
            idempotency_key="old",
            request_fingerprint="f",
            completed=True,
            created_at=datetime.now(UTC) - timedelta(days=2),
        )
    )
    db_session.add(
        models.IdempotencyRecord(
            api_key_hash="h",
            idempotency_key="new",
            request_fingerprint="f",
            completed=True,
        )
    )
    db_session.commit()
    deleted = crud.purge_idempotency_records(db_session, older_than_seconds=86_400)
    assert deleted == 1  # only the 2-day-old record
    assert db_session.query(models.IdempotencyRecord).count() == 1


def test_reconcile_clean_after_idempotent_ops(db_session):
    _, key = auth.create_user_with_key(db_session, "e", credits=10)
    client = TestClient(make_app())
    h = {"X-API-Key": key, "Idempotency-Key": "x"}
    client.get("/hello", headers=h)
    client.get("/hello", headers=h)
    db_session.expire_all()  # charge committed on the dependency's own session
    assert crud.find_balance_discrepancies(db_session) == []
