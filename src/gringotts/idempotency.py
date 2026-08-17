"""Response-caching idempotency: retry a mutating request safely.

A client that retries a request it isn't sure completed (a dropped connection, a
timeout) must not be charged or granted twice. The mechanism is an ``Idempotency-
Key`` header plus this ASGI middleware: the first request with a given key runs
normally and its response is captured into ``idempotency_records``; any later
request carrying the same key from the same caller gets that stored response back
without re-running the handler — so the charge (and any other side effect in the
handler) happens exactly once. Responses that are unsafe or too large to retain
replay a marker instead of the original body, while still keeping the key locked.

Because the replay short-circuits above the route, it also means a retry can never
double-charge, never re-run the handler for free, and never race the original's
refund — the handler simply does not execute a second time.

Design choices (documented, not incidental):

- **Only authenticated callers create records.** The API key is validated before
  any row is inserted, so an unauthenticated client can't fill the table; its
  request passes through to the app's own ``401`` (which is not cached).
- **Scope is the caller** (SHA-256 of the ``X-API-Key``), so one caller can never
  replay another caller's key.
- **A fingerprint** (method + path + query + every request header + body) guards
  the key: changing authorization, pricing, content, or any other input returns
  ``409`` rather than replaying a response produced for different input.
- **Complete compensation releases the key; a committed charge keeps it.** gringotts
  charges *before* the handler runs, and ``Depends(charge())`` compensates on a
  raised exception. The key is released only after every charge from the request has
  a confirmed refund for the exact total, so a genuine retry can re-attempt. If any
  compensation fails, the key stays locked because a debit still stands. Any response
  the handler *returns* (2xx, 4xx, even a 5xx it returns itself) leaves the charge
  committed and is cached, so a retry can't run it again. A caller who wants a fresh
  attempt uses a fresh key.
- **An in-flight or crashed request is never re-run automatically.** A concurrent
  duplicate, and any retry of a request whose outcome is unknown, gets ``409`` —
  because age alone can't prove the first attempt didn't already charge. Records
  expire after ``retention_seconds`` so a key is eventually reusable and the table
  doesn't grow without bound; operators can also purge with ``gringotts
  prune-idempotency``.
- **Bounded buffering.** A request body over ``max_body_bytes`` is rejected with
  ``413`` before the app runs. A response over ``max_response_bytes`` reaches the
  client normally, then replays a small marker on retries. A client disconnect
  mid-upload runs nothing and stores nothing.
- **Client disconnects are not cached** and never release a committed charge: a
  ``CancelledError`` raised as the response streams is left to propagate, so the
  record stays and the retry gets ``409`` rather than risking a re-charge.

Limitations (deliberate trade-offs for a money library):

- **Only the charge is guaranteed exactly-once.** A handler that raises *before*
  responding has its charge refunded by ``Depends(charge())``, so the key is
  released and a genuine retry re-attempts — that is what makes transient failures
  safe to retry. Any *other* side effect the handler commits before raising is not
  rolled back by gringotts and could run again on that retry; such side effects
  must be made idempotent by the application (e.g. its own key or a unique
  constraint).
- **A crashed or disconnected in-flight request leaks its lock.** The record stays
  ``completed=False`` (a ``409`` for retries, never re-run — its outcome is
  unknown), and ``prune-idempotency`` retains in-flight rows by default so it can't
  free a still-running request's key. Clear abandoned locks during a maintenance
  window with ``prune-idempotency --include-in-flight``.
"""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from . import auth, db
from .models import IdempotencyRecord, User

_JSON = [(b"content-type", b"application/json")]
_CONFLICT_BODY = json.dumps(
    {"detail": "Idempotency-Key reused with a different request"}
).encode()
_IN_PROGRESS_BODY = json.dumps(
    {"detail": "A request with this Idempotency-Key is still in progress"}
).encode()
_KEY_TOO_LONG_BODY = json.dumps({"detail": "Idempotency-Key is too long"}).encode()
_BODY_TOO_LARGE_BODY = json.dumps(
    {"detail": "Request body too large for an idempotent request"}
).encode()
# Stored for a request whose response was too large to cache: the charge already
# happened once, so a replay must return this (keeping the key locked) rather than
# re-running the handler.
_MARKER_BODY = json.dumps(
    {"detail": "The request was processed; its response was not cached for replay."}
).encode()


def _fingerprint(
    method: str,
    path: str,
    query: bytes,
    body: bytes,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> str:
    """SHA-256 over the parts of a request that define its identity.

    Each part is length-prefixed so the encoding is injective — a separator byte
    alone is not enough, because a part may itself contain that byte, letting a
    crafted (query, body) collide with a different (query, body) and be wrongly
    deduplicated.
    """
    digest = hashlib.sha256()
    parts = [method.encode(), path.encode(), query, body]
    request_headers = headers or []
    parts.append(len(request_headers).to_bytes(8, "big"))
    for name, value in request_headers:
        parts.extend((name.lower(), value))
    for part in parts:
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


class IdempotencyMiddleware:
    """ASGI middleware that caches and replays responses keyed by Idempotency-Key.

    Attributes:
        app: The wrapped ASGI application.
        header: The header carrying the idempotency key.
        max_key_length: Keys longer than this are rejected with ``400``.
        max_body_bytes: A request body larger than this is rejected with ``413``.
        max_response_bytes: A response larger than this is streamed but not cached.
        retention_seconds: A record older than this is treated as expired.
    """

    def __init__(
        self,
        app,
        *,
        header: str = "Idempotency-Key",
        max_key_length: int = 255,
        max_body_bytes: int = 1_000_000,
        max_response_bytes: int = 1_000_000,
        retention_seconds: float = 86_400.0,
    ) -> None:
        """Wrap `app`, reading `header` and honoring the size and age limits."""
        self.app = app
        self.header = header.lower().encode("latin-1")
        self.api_key_header = b"x-api-key"
        self.max_key_length = max_key_length
        self.max_body_bytes = max_body_bytes
        self.max_response_bytes = max_response_bytes
        self.retention_seconds = retention_seconds

    async def __call__(self, scope, receive, send) -> None:
        """Intercept a keyed HTTP request; otherwise pass straight through."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Take the FIRST occurrence of each header, matching what FastAPI's
        # request.headers.get() authenticates on — dict() would pick the last, so
        # a duplicated X-API-Key could scope under a different caller than the one
        # the handler charges.
        raw_key = raw_api_key = None
        for name, value in scope["headers"]:
            lname = name.lower()
            if lname == self.header and raw_key is None:
                raw_key = value
            elif lname == self.api_key_header and raw_api_key is None:
                raw_api_key = value
        # No key, or no caller to scope it to → not our concern.
        if not raw_key or not raw_api_key:
            await self.app(scope, receive, send)
            return

        if len(raw_key) > self.max_key_length:
            await self._send(send, 400, _KEY_TOO_LONG_BODY, _JSON)
            return

        api_key = raw_api_key.decode("latin-1")
        # Validate the caller before touching the table: an invalid key must not
        # be able to create records, so it just falls through to the app's 401.
        if not await run_in_threadpool(self._caller_exists, api_key):
            await self.app(scope, receive, send)
            return

        key = raw_key.decode("latin-1")
        api_key_hash = auth.get_api_key_hash(api_key)
        body, state = await self._read_body(receive)
        if state == "disconnect":
            # The client aborted mid-upload. Run nothing (so no charge happens) and
            # cache nothing — never disguise a truncated request as a completed one.
            return
        if state == "overflow":
            # We can't fingerprint or cache a body we won't fully buffer, and
            # running it uncached would break exactly-once — so reject it outright
            # before any side effect, rather than silently downgrading.
            await self._send(send, 413, _BODY_TOO_LARGE_BODY, _JSON)
            return

        # Any request header can affect authentication, pricing, or handler output.
        # Bind all of them except the idempotency key itself into the operation
        # fingerprint so a changed Authorization/Cookie/custom header conflicts
        # instead of replaying a response produced for different input.
        fingerprint_headers = [
            (name, value)
            for name, value in scope["headers"]
            if name.lower() != self.header
        ]
        fingerprint = _fingerprint(
            scope["method"],
            scope["path"],
            scope.get("query_string", b""),
            body,
            fingerprint_headers,
        )
        outcome, claim, stored = await run_in_threadpool(
            self._claim, api_key_hash, key, fingerprint
        )

        if outcome == "conflict":
            await self._send(send, 409, _CONFLICT_BODY, _JSON)
            return
        if outcome == "in_progress":
            await self._send(send, 409, _IN_PROGRESS_BODY, _JSON)
            return
        if outcome == "replay":
            assert stored is not None  # noqa: S101 - guaranteed by the outcome
            status, resp_body, resp_headers = stored
            await self._send_replay(send, status or 200, resp_body or b"", resp_headers)
            return

        # outcome == "new": run the app, capturing its response for storage.
        assert claim is not None  # noqa: S101 - guaranteed by a "new" outcome

        replayed = {"done": False}

        async def replay_receive():
            # Replay the buffered body once, then delegate to the real receive so a
            # streaming response still sees http.disconnect instead of spinning on
            # an endlessly-repeated terminal message.
            if not replayed["done"]:
                replayed["done"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        captured = {
            "status": 500,
            "headers": [],
            "body": bytearray(),
            "too_big": False,
            "started": False,
            "finished": False,
        }

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                captured["started"] = True
                captured["status"] = message["status"]
                captured["headers"] = message["headers"]
            elif message["type"] == "http.response.body":
                if not captured["too_big"]:
                    chunk = message.get("body", b"")
                    # Check the prospective size BEFORE copying, so a single huge
                    # chunk can't blow past the cap into memory.
                    if len(captured["body"]) + len(chunk) > self.max_response_bytes:
                        captured["too_big"] = True
                        captured["body"] = bytearray()  # release what we held
                    else:
                        captured["body"].extend(chunk)
                # The terminal frame (more_body False/absent) marks a complete
                # response; an interrupted stream never sends it.
                if not message.get("more_body", False):
                    captured["finished"] = True
            await send(message)

        try:
            await self.app(scope, replay_receive, send_wrapper)
        except Exception:
            # Release the key ONLY when every charge is fully compensated. A
            # committed-but-not-refunded charge — including one of several whose
            # refund failed — must keep the key locked so a retry can't double it.
            if _refunded(scope):
                await run_in_threadpool(self._delete, claim)
            else:
                await run_in_threadpool(self._persist, claim, captured)
            raise

        # The handler RETURNED. Release the key only when no net charge stands:
        #  - a confirmed refund (e.g. an HTTPException turned into a 5xx) → retryable
        #  - a FastAPI slash-redirect that ran NO handler (so no charge committed) →
        #    release so the client's redirect-follow isn't a 409.
        # A committed charge (even one that then returned a redirect) is cached so a
        # retry can never re-run it.
        released = _refunded(scope)
        committed = _committed(scope)
        slash = (
            not committed
            # FastAPI adds ``endpoint`` only after a route matches and runs. Its
            # automatic slash redirect happens before that; requiring the marker
            # prevents an endpoint's own same-path redirect from releasing the
            # claim and repeating an uncharged side effect.
            and "endpoint" not in scope
            and _is_slash_redirect(
                captured["status"], captured["headers"], scope["path"]
            )
        )
        if released or slash:
            await run_in_threadpool(self._delete, claim)
        else:
            await run_in_threadpool(self._persist, claim, captured)

    async def _read_body(self, receive):
        """Drain the request body, bounded, detecting a mid-upload disconnect.

        Returns ``(body, state)`` where state is ``ok`` / ``overflow`` /
        ``disconnect``. A keyed request is only ever run when the whole body was
        read cleanly (``ok``); the other two states are handled without running
        the app, so a partial or unbounded request can't slip through uncached.
        """
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return b"".join(chunks), "disconnect"
            chunk = message.get("body", b"")
            # Check the prospective size BEFORE buffering the frame, and don't join
            # what we hold on overflow — the request is rejected, so the bytes are
            # never needed, and neither the append nor the join should copy them.
            if total + len(chunk) > self.max_body_bytes:
                return b"", "overflow"
            if chunk:
                chunks.append(chunk)
                total += len(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks), "ok"

    async def _send(self, send, status: int, body: bytes, headers) -> None:
        """Send a complete response without invoking the wrapped app."""
        out = [*headers, (b"content-length", str(len(body)).encode("latin-1"))]
        await send({"type": "http.response.start", "status": status, "headers": out})
        await send({"type": "http.response.body", "body": body})

    async def _send_replay(self, send, status: int, body: bytes, headers_json) -> None:
        """Replay a stored response: its headers, a fresh length, a replay marker.

        We send one complete body, so any framing from the original — the stored
        ``Content-Length`` or a ``Transfer-Encoding: chunked`` from a streaming
        response — is dropped and recomputed; sending both would violate RFC 9112
        and crash strict ASGI servers. A bodyless status (204/304/1xx) gets no
        ``Content-Length`` at all, which RFC 9110 forbids there.
        """
        headers = [
            (name, value)
            for name, value in _load_headers(headers_json)
            if name.lower() not in (b"content-length", b"transfer-encoding")
        ]
        if not _is_bodyless(status):
            headers.append((b"content-length", str(len(body)).encode("latin-1")))
        headers.append((b"idempotent-replayed", b"true"))
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": body})

    def _caller_exists(self, api_key: str) -> bool:
        """Whether the API key belongs to a real account. Runs in a worker thread."""
        session = db.SessionLocal()
        try:
            hash_ = auth.get_api_key_hash(api_key)
            return (
                session.query(User.id).filter(User.api_key_hash == hash_).first()
                is not None
            )
        finally:
            session.close()

    def _claim(self, api_key_hash: str, key: str, fingerprint: str):
        """Reserve the key or report its prior state. Runs in a worker thread.

        Returns ``(outcome, claim, stored)`` where outcome is one of
        ``new`` / ``replay`` / ``conflict`` / ``in_progress``. An in-progress
        record is never reclaimed by age — an unknown outcome stays a ``409``.
        A new claim is ``(record_id, created_at)`` so later writes can reject a
        primary key that has been deleted and reused for another request.
        """
        session = db.SessionLocal()
        try:
            record = self._get(session, api_key_hash, key)
            if record is not None and record.completed and self._expired(record):
                # A key past its retention is reusable. Reclaim the row IN PLACE
                # with a guarded UPDATE rather than delete+insert: on SQLite the
                # deleted INTEGER PRIMARY KEY can be reused, so a delete+insert race
                # could let a second request delete the first's fresh claim. The
                # created_at guard means exactly one concurrent racer wins the
                # reclaim; the loser (rowcount 0) falls through and sees the winner's
                # in-progress row -> 409.
                now = datetime.now(UTC)
                reclaimed = (
                    session.query(IdempotencyRecord)
                    .filter(
                        IdempotencyRecord.id == record.id,
                        IdempotencyRecord.created_at == record.created_at,
                        IdempotencyRecord.completed.is_(True),
                    )
                    .update(
                        {
                            IdempotencyRecord.completed: False,
                            IdempotencyRecord.request_fingerprint: fingerprint,
                            IdempotencyRecord.created_at: now,
                            IdempotencyRecord.status_code: None,
                            IdempotencyRecord.response_body: None,
                            IdempotencyRecord.response_headers: None,
                        },
                        synchronize_session=False,
                    )
                )
                session.commit()
                if reclaimed:
                    return ("new", (record.id, now), None)
                # Lost the reclaim race: re-read and classify the winner's row.
                # A concurrent prune can instead have removed the completed row;
                # in that case fall through to the normal insert race below.
                record = self._get(session, api_key_hash, key)
            if record is None:
                fresh = IdempotencyRecord(
                    api_key_hash=api_key_hash,
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    completed=False,
                )
                session.add(fresh)
                try:
                    session.commit()
                except IntegrityError:
                    # A concurrent first request won the insert race; classify it.
                    session.rollback()
                    record = self._get(session, api_key_hash, key)
                    if record is None:  # pragma: no cover - vanished mid-race
                        return ("in_progress", None, None)
                else:
                    return ("new", (fresh.id, fresh.created_at), None)

            if record.request_fingerprint != fingerprint:
                return ("conflict", None, None)
            if record.completed:
                return (
                    "replay",
                    None,
                    (record.status_code, record.response_body, record.response_headers),
                )
            return ("in_progress", None, None)
        finally:
            session.close()

    def _persist(self, claim: tuple[int, datetime], captured: dict) -> None:
        """Store a handler's captured response under a record. Worker thread.

        The body is replaced with a small JSON marker (keeping the key locked so a
        retry can't re-run a committed side effect) when it can't or shouldn't be
        served verbatim: too large to cache, an interrupted stream (partial body),
        or a ``Cache-Control: no-store`` response (e.g. one that issued a
        credential — never persist that body). Side-effect headers such as
        ``Location`` / ``Set-Cookie`` are kept, but representation headers
        (content-type/length/encoding) are dropped since the body is now JSON.
        """
        no_store = _has_no_store(captured["headers"])
        if captured["too_big"] or not captured["finished"] or no_store:
            kept = [
                (name, value)
                for name, value in captured["headers"]
                if name.lower()
                not in (b"content-length", b"content-type", b"content-encoding")
            ]
            if no_store:
                # Never persist a no-store body (it may hold a secret) or its
                # side-effect headers (e.g. Set-Cookie); keep only the status.
                kept = []
            # A 204/304/1xx status forbids a body — replaying a marker body with a
            # positive Content-Length there is malformed and servers may reject it.
            if _is_bodyless(captured["status"]):
                self._store(
                    claim,
                    captured["status"],
                    b"",
                    _dump_headers(
                        kept, extra=[("idempotent-response-not-cached", "true")]
                    ),
                )
            else:
                self._store(
                    claim,
                    captured["status"],
                    _MARKER_BODY,
                    _dump_headers(
                        kept,
                        extra=[
                            ("content-type", "application/json"),
                            ("idempotent-response-not-cached", "true"),
                        ],
                    ),
                )
        else:
            self._store(
                claim,
                captured["status"],
                bytes(captured["body"]),
                _dump_headers(captured["headers"]),
            )

    def _store(
        self,
        claim: tuple[int, datetime],
        status: int,
        body: bytes,
        headers_json: str,
    ) -> None:
        """Mark this exact claim complete with its captured response."""
        record_id, created_at = claim
        session = db.SessionLocal()
        try:
            # The creation timestamp is the claim generation. If an operator
            # explicitly purged this in-flight row and its integer PK was reused,
            # the old request must not complete the new owner's record.
            session.query(IdempotencyRecord).filter(
                IdempotencyRecord.id == record_id,
                IdempotencyRecord.created_at == created_at,
                IdempotencyRecord.completed.is_(False),
            ).update(
                {
                    IdempotencyRecord.completed: True,
                    IdempotencyRecord.status_code: status,
                    IdempotencyRecord.response_body: body,
                    IdempotencyRecord.response_headers: headers_json,
                },
                synchronize_session=False,
            )
            session.commit()
        finally:
            session.close()

    def _delete(self, claim: tuple[int, datetime]) -> None:
        """Drop this exact in-progress claim so its key can be retried.

        A filtered bulk delete (not ``session.delete``) so a concurrent delete of
        the same row can't raise StaleDataError. The generation guard prevents an
        old request from deleting a newer claim if its primary key was reused.
        """
        record_id, created_at = claim
        session = db.SessionLocal()
        try:
            session.query(IdempotencyRecord).filter(
                IdempotencyRecord.id == record_id,
                IdempotencyRecord.created_at == created_at,
                IdempotencyRecord.completed.is_(False),
            ).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

    @staticmethod
    def _get(session, api_key_hash: str, key: str):
        return (
            session.query(IdempotencyRecord)
            .filter(
                IdempotencyRecord.api_key_hash == api_key_hash,
                IdempotencyRecord.idempotency_key == key,
            )
            .first()
        )

    def _expired(self, record) -> bool:
        created = record.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return (datetime.now(UTC) - created).total_seconds() > self.retention_seconds


def _dump_headers(headers, extra=None) -> str:
    """JSON-encode an ASGI header list (dropping length, recomputed on replay).

    `extra` adds extra ``(name, value)`` string pairs after the captured headers.
    """
    pairs = [
        [name.decode("latin-1"), value.decode("latin-1")]
        for name, value in headers
        # content-length is recomputed on replay; transfer-encoding (chunked) is
        # meaningless for a stored complete body and would collide with it.
        if name.lower() not in (b"content-length", b"transfer-encoding")
    ]
    if extra:
        pairs.extend([list(pair) for pair in extra])
    return json.dumps(pairs)


def _is_bodyless(status: int) -> bool:
    """Whether an HTTP status forbids a response body (204, 304, 1xx)."""
    return status in (204, 304) or 100 <= status < 200


def _refunded(scope) -> bool:
    """Whether every charge in this request has an exact confirmed refund."""
    state = scope.get("gringotts_idempotency", {})
    charge_count = state.get("charge_count", 0)
    return (
        charge_count > 0
        and state.get("refund_count", 0) == charge_count
        and state.get("refunded_credits", 0) == state.get("charged_credits", 0)
    )


def _committed(scope) -> bool:
    """Whether at least one charge dependency committed in this request."""
    return scope.get("gringotts_idempotency", {}).get("charge_count", 0) > 0


def _is_slash_redirect(status: int, headers, path: str) -> bool:
    """Whether this is FastAPI's automatic add/remove-trailing-slash redirect.

    Such a redirect is issued by the router *before* the endpoint runs (so no
    charge happened) and points at the same path with the slash toggled. Caching
    it would make the client's redirect-follow — same key, the other path, a
    different fingerprint — collide as a 409. Matched narrowly so a handler's own
    redirect elsewhere is still cached.
    """
    if status not in (307, 308):
        return False
    loc = None
    for name, value in headers:
        if name.lower() == b"location":
            loc = value.decode("latin-1")
            break
    if loc is None:
        return False
    loc_path = loc.split("?", 1)[0]
    if "://" in loc_path:  # strip scheme://host, leaving the path
        rest = loc_path.split("://", 1)[1]
        loc_path = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    # same path modulo a toggled trailing slash (not an identical URL)
    return loc_path != path and loc_path.rstrip("/") == path.rstrip("/")


def _has_no_store(headers) -> bool:
    """Whether the response asked not to be stored (``Cache-Control: no-store``)."""
    for name, value in headers:
        if name.lower() == b"cache-control" and b"no-store" in value.lower():
            return True
    return False


def _load_headers(headers_json: str | None):
    """Decode stored headers back to a list of (bytes, bytes) ASGI pairs."""
    if not headers_json:
        return []
    return [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in json.loads(headers_json)
    ]
