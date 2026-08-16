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

- **Scope is the caller** (SHA-256 of the ``X-API-Key``), so one caller can never
  replay another caller's key. A request with a key but no API key is passed
  through untouched (there is no caller to scope to).
- **A fingerprint** (method + path + query + body) guards the key: reusing it for
  a materially different request returns ``409`` rather than silently returning
  the wrong cached response.
- **5xx responses and unhandled exceptions are not cached** — they are transient,
  so the key is released and a genuine retry re-attempts. 4xx (including a ``402``
  for insufficient credits) *is* cached: it is a deterministic result of that
  request, and a caller who wants a fresh attempt uses a fresh key.
- **A crashed in-flight request** leaves an ``in_progress`` row; a concurrent
  duplicate gets ``409`` while it is fresh, and the row becomes reclaimable after
  ``in_progress_ttl`` so a crash can't lock a key forever.
"""

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from . import auth, db
from .models import IdempotencyRecord

_CONFLICT_BODY = json.dumps(
    {"detail": "Idempotency-Key reused with a different request"}
).encode()
_IN_PROGRESS_BODY = json.dumps(
    {"detail": "A request with this Idempotency-Key is still in progress"}
).encode()
_KEY_TOO_LONG_BODY = json.dumps({"detail": "Idempotency-Key is too long"}).encode()


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
        max_key_length: Keys longer than this are rejected with ``400`` (an
            unbounded key would land in a unique index and could exceed a
            backend's index-entry limit).
        in_progress_ttl: Seconds after which an ``in_progress`` record is treated
            as abandoned (its owner crashed) and may be reclaimed.
    """

    def __init__(
        self,
        app,
        *,
        header: str = "Idempotency-Key",
        max_key_length: int = 255,
        in_progress_ttl: float = 90.0,
    ) -> None:
        """Wrap `app`, reading `header` and honoring the two limits."""
        self.app = app
        self.header = header.lower().encode("latin-1")
        self.api_key_header = b"x-api-key"
        self.max_key_length = max_key_length
        self.in_progress_ttl = in_progress_ttl

    async def __call__(self, scope, receive, send) -> None:
        """Intercept a keyed HTTP request; otherwise pass straight through."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        raw_key = headers.get(self.header)
        raw_api_key = headers.get(self.api_key_header)
        # No key, or no caller to scope it to → not our concern.
        if not raw_key or not raw_api_key:
            await self.app(scope, receive, send)
            return

        if len(raw_key) > self.max_key_length:
            await self._send_json(send, 400, _KEY_TOO_LONG_BODY)
            return

        key = raw_key.decode("latin-1")
        api_key_hash = auth.get_api_key_hash(raw_api_key.decode("latin-1"))
        body = await _read_body(receive)
        fingerprint = _fingerprint(
            scope["method"], scope["path"], scope.get("query_string", b""), body
        )

        outcome, record_id, stored = await run_in_threadpool(
            self._claim, api_key_hash, key, fingerprint
        )

        if outcome == "conflict":
            await self._send_json(send, 409, _CONFLICT_BODY)
            return
        if outcome == "in_progress":
            await self._send_json(send, 409, _IN_PROGRESS_BODY)
            return
        if outcome == "replay":
            assert stored is not None  # noqa: S101 - guaranteed by the claim outcome
            status, resp_body, media_type = stored
            await self._send_json(
                send, status or 200, resp_body or b"", media_type, replayed=True
            )
            return

        # outcome == "new": run the app, capturing its response for storage.
        assert record_id is not None  # noqa: S101 - guaranteed by a "new" outcome

        async def replay_receive():
            return {"type": "http.request", "body": body, "more_body": False}

        captured = {"status": 500, "headers": [], "body": bytearray()}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                captured["headers"] = message["headers"]
            elif message["type"] == "http.response.body":
                captured["body"].extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, replay_receive, send_wrapper)
        except Exception:
            # Unhandled error: release the key so a real retry re-attempts, then
            # let ServerErrorMiddleware (outside us) produce the 500.
            await run_in_threadpool(self._delete, record_id)
            raise

        if captured["status"] >= 500:
            await run_in_threadpool(self._delete, record_id)
        else:
            media_type = _content_type(captured["headers"])
            await run_in_threadpool(
                self._store,
                record_id,
                captured["status"],
                bytes(captured["body"]),
                media_type,
            )

    async def _send_json(
        self,
        send,
        status: int,
        body: bytes,
        media_type: str | None = "application/json",
        replayed: bool = False,
    ) -> None:
        """Send a complete response without invoking the wrapped app."""
        headers = [
            (b"content-type", (media_type or "application/json").encode("latin-1")),
            (b"content-length", str(len(body)).encode("latin-1")),
        ]
        if replayed:
            headers.append((b"idempotent-replayed", b"true"))
        await send(
            {"type": "http.response.start", "status": status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": body})

    def _claim(self, api_key_hash: str, key: str, fingerprint: str):
        """Reserve the key or report its prior state. Runs in a worker thread.

        Returns ``(outcome, record_id, stored)`` where outcome is one of
        ``new`` / ``replay`` / ``conflict`` / ``in_progress``.
        """
        session = db.SessionLocal()
        try:
            record = (
                session.query(IdempotencyRecord)
                .filter(
                    IdempotencyRecord.api_key_hash == api_key_hash,
                    IdempotencyRecord.idempotency_key == key,
                )
                .first()
            )
            if record is None:
                record = IdempotencyRecord(
                    api_key_hash=api_key_hash,
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    completed=False,
                )
                session.add(record)
                try:
                    session.commit()
                except IntegrityError:
                    # A concurrent first request won the insert race; fall through
                    # to treat that winner's row as the existing record.
                    session.rollback()
                    record = (
                        session.query(IdempotencyRecord)
                        .filter(
                            IdempotencyRecord.api_key_hash == api_key_hash,
                            IdempotencyRecord.idempotency_key == key,
                        )
                        .first()
                    )
                    if record is None:  # pragma: no cover - vanished mid-race
                        return ("in_progress", None, None)
                else:
                    return ("new", record.id, None)

            if record.request_fingerprint != fingerprint:
                return ("conflict", None, None)
            if record.completed:
                return (
                    "replay",
                    None,
                    (
                        record.status_code,
                        record.response_body,
                        record.response_media_type,
                    ),
                )
            # In progress: reclaim only if the owner looks to have died, and only
            # if we win a guarded update so two reclaimers can't both proceed.
            created = record.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if (datetime.now(UTC) - created).total_seconds() > self.in_progress_ttl:
                reclaimed = (
                    session.query(IdempotencyRecord)
                    .filter(
                        IdempotencyRecord.id == record.id,
                        IdempotencyRecord.created_at == record.created_at,
                        IdempotencyRecord.completed.is_(False),
                    )
                    .update(
                        {
                            IdempotencyRecord.request_fingerprint: fingerprint,
                            IdempotencyRecord.created_at: datetime.now(UTC),
                        }
                    )
                )
                session.commit()
                if reclaimed:
                    return ("new", record.id, None)
            return ("in_progress", None, None)
        finally:
            session.close()

    def _store(
        self, record_id: int, status: int, body: bytes, media_type: str | None
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
            record.response_media_type = media_type
            session.commit()
        finally:
            session.close()

    def _delete(self, record_id: int) -> None:
        """Drop an in-progress record so its key can be retried. Worker thread."""
        session = db.SessionLocal()
        try:
            record = session.get(IdempotencyRecord, record_id)
            if record is not None:
                session.delete(record)
                session.commit()
        finally:
            session.close()


async def _read_body(receive) -> bytes:
    """Drain an ASGI request body into a single bytes buffer."""
    chunks = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _content_type(headers) -> str | None:
    """Pull the content-type value out of a captured ASGI header list."""
    for name, value in headers:
        if name.lower() == b"content-type":
            return value.decode("latin-1")
    return None
