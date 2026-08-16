"""Response-caching idempotency: retry a mutating request safely.

A client that retries a request it isn't sure completed (a dropped connection, a
timeout) must not be charged or granted twice. The mechanism is an ``Idempotency-
Key`` header plus this ASGI middleware: the first request with a given key runs
normally and its response is captured into ``idempotency_records``; any later
request carrying the same key from the same caller gets that stored response back
**verbatim, without re-running the handler** — so the charge (and any other side
effect in the handler) happens exactly once.

Because the replay short-circuits above the route, it also means a retry can never
double-charge, never re-run the handler for free, and never race the original's
refund — the handler simply does not execute a second time.

Design choices (documented, not incidental):

- **Only authenticated callers create records.** The API key is validated before
  any row is inserted, so an unauthenticated client can't fill the table; its
  request passes through to the app's own ``401`` (which is not cached).
- **Scope is the caller** (SHA-256 of the ``X-API-Key``), so one caller can never
  replay another caller's key.
- **A fingerprint** (method + path + query + body) guards the key: reusing it for
  a materially different request returns ``409`` rather than the wrong cached
  response.
- **A raised exception releases the key; a returned response is cached.** gringotts
  charges *before* the handler runs, and ``Depends(charge())`` refunds on a raised
  exception — so a handler that raises is treated as rolled back and a genuine retry
  re-attempts, while any response the handler *returns* (2xx, 4xx, even a 5xx it
  returns itself) leaves the charge committed and is cached, so a retry can't run it
  again. A caller who wants a fresh attempt uses a fresh key.
- **An in-flight or crashed request is never re-run automatically.** A concurrent
  duplicate, and any retry of a request whose outcome is unknown, gets ``409`` —
  because age alone can't prove the first attempt didn't already charge. Records
  expire after ``retention_seconds`` so a key is eventually reusable and the table
  doesn't grow without bound; operators can also purge with ``gringotts
  prune-idempotency``.
- **Bounded buffering.** A request body over ``max_body_bytes`` or a response over
  ``max_response_bytes`` is streamed through uncached rather than held in memory,
  and a client disconnect mid-upload passes through without caching a truncated
  request.
- **Client disconnects are not cached** and never release a committed charge: a
  ``CancelledError`` raised as the response streams is left to propagate, so the
  record stays and the retry gets ``409`` rather than risking a re-charge.
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
_TRUNCATED_BODY = json.dumps(
    {"detail": "The request was processed; its response was too large to cache."}
).encode()


def _fingerprint(method: str, path: str, query: bytes, body: bytes) -> str:
    """SHA-256 over the parts of a request that define its identity."""
    digest = hashlib.sha256()
    for part in (method.encode(), path.encode(), query, body):
        digest.update(part)
        digest.update(b"\x00")
    return digest.hexdigest()


class IdempotencyMiddleware:
    """ASGI middleware that caches and replays responses keyed by Idempotency-Key.

    Attributes:
        app: The wrapped ASGI application.
        header: The header carrying the idempotency key.
        max_key_length: Keys longer than this are rejected with ``400``.
        max_body_bytes: A request body larger than this is passed through uncached.
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

        fingerprint = _fingerprint(
            scope["method"], scope["path"], scope.get("query_string", b""), body
        )
        outcome, record_id, stored = await run_in_threadpool(
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
        assert record_id is not None  # noqa: S101 - guaranteed by a "new" outcome

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
            await send(message)

        try:
            await self.app(scope, replay_receive, send_wrapper)
        except Exception:
            if captured["started"]:
                # The response already began, so the charge is committed and the
                # client got (most of) its result — e.g. a BackgroundTask or a
                # send() raised after the handler returned. Deleting the key would
                # let a retry re-charge, so cache the outcome and keep it locked.
                await run_in_threadpool(self._persist, record_id, captured)
            else:
                # The handler raised before responding. Under the documented pattern
                # (Depends(charge())) that is refunded by the dependency, so release
                # the key for a genuine retry; ServerErrorMiddleware (outside us)
                # then produces the 500. A CancelledError from a client disconnect
                # is a BaseException, so it isn't caught here — the record stays and
                # the retry gets 409 rather than risking a re-charge.
                await run_in_threadpool(self._delete, record_id)
            raise

        # The handler RETURNED (any status, 5xx included). gringotts charges before
        # the handler runs, so whatever it returns, the side effect is committed and
        # not rolled back — cache the outcome so a retry can't run it again. Only a
        # pre-response raised exception (above) releases the key.
        await run_in_threadpool(self._persist, record_id, captured)

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
        """Replay a stored response: its headers, a fresh length, a replay marker."""
        headers = [
            (name, value)
            for name, value in _load_headers(headers_json)
            if name.lower() != b"content-length"
        ]
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

        Returns ``(outcome, record_id, stored)`` where outcome is one of
        ``new`` / ``replay`` / ``conflict`` / ``in_progress``. An in-progress
        record is never reclaimed by age — an unknown outcome stays a ``409``.
        """
        session = db.SessionLocal()
        try:
            record = self._get(session, api_key_hash, key)
            if record is not None and record.completed and self._expired(record):
                # A key past its retention is reusable; drop it and start fresh. Use
                # a filtered bulk delete (not session.delete) so two concurrent
                # requests both expiring the same row don't raise StaleDataError.
                session.query(IdempotencyRecord).filter(
                    IdempotencyRecord.id == record.id
                ).delete(synchronize_session=False)
                session.commit()
                record = None
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
                    return ("new", fresh.id, None)

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

    def _persist(self, record_id: int, captured: dict) -> None:
        """Store a handler's captured response under a record. Worker thread.

        A response too large to cache is stored as a small marker body while
        keeping the original headers (so a redirect's ``Location`` or a
        ``Set-Cookie`` still replays) plus an ``idempotent-response-truncated``
        flag — the point is to keep the key locked so a retry can't re-run a
        committed side effect, not to reproduce the oversized body.
        """
        if captured["too_big"]:
            # keep side-effect headers (Location, Set-Cookie), but the body is now a
            # JSON marker, so drop the original content-type and set it to match.
            kept = [
                (name, value)
                for name, value in captured["headers"]
                if name.lower() not in (b"content-length", b"content-type")
            ]
            headers = _dump_headers(
                kept,
                extra=[
                    ("content-type", "application/json"),
                    ("idempotent-response-truncated", "true"),
                ],
            )
            self._store(record_id, captured["status"], _TRUNCATED_BODY, headers)
        else:
            self._store(
                record_id,
                captured["status"],
                bytes(captured["body"]),
                _dump_headers(captured["headers"]),
            )

    def _store(
        self, record_id: int, status: int, body: bytes, headers_json: str
    ) -> None:
        """Mark a record complete with its captured response. Worker thread."""
        session = db.SessionLocal()
        try:
            record = session.get(IdempotencyRecord, record_id)
            if record is None:  # pragma: no cover - reclaimed/deleted concurrently
                return
            record.completed = True
            record.status_code = status
            record.response_body = body
            record.response_headers = headers_json
            session.commit()
        finally:
            session.close()

    def _delete(self, record_id: int) -> None:
        """Drop an in-progress record so its key can be retried. Worker thread.

        A filtered bulk delete (not ``session.delete``) so a concurrent delete of
        the same row can't raise StaleDataError.
        """
        session = db.SessionLocal()
        try:
            session.query(IdempotencyRecord).filter(
                IdempotencyRecord.id == record_id
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
        if name.lower() != b"content-length"
    ]
    if extra:
        pairs.extend([list(pair) for pair in extra])
    return json.dumps(pairs)


def _load_headers(headers_json: str | None):
    """Decode stored headers back to a list of (bytes, bytes) ASGI pairs."""
    if not headers_json:
        return []
    return [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in json.loads(headers_json)
    ]
