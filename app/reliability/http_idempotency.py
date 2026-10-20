"""HTTP idempotency for every POST under /api/v1 and /connector/v1 (IS4).

Per request with ``Idempotency-Key: K`` (scope = ``POST <path>``):

1. open a DB transaction and take ``pg_advisory_xact_lock(scope, K)``; a concurrent duplicate waits here
   (at most ``IDEMPOTENCY_LOCK_WAIT_S``, then 409 IDEMPOTENCY_IN_PROGRESS);
2. a stored row with the same body hash → replay the stored status + body (``Idempotent-Replayed: true``);
   a different body hash → 409 IDEMPOTENCY_KEY_REUSED;
3. otherwise run the endpoint *inside the same transaction* (``get_session`` joins it), store the 2xx
   response in ``idempotency_keys`` and commit once: the job row and the key row land together or not at all.

Only 2xx responses are stored (plus errors an endpoint explicitly keeps); an error rolls back and a retry runs again. A missing header gets a
generated key, echoed in ``Idempotency-Key`` with ``X-IncuBrix-Warning: IDEMPOTENCY_KEY_GENERATED``.
Connector-contract routes are excluded (see ``is_contract_call``): publish dedups in the publish-intent ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, insert, select, text
from sqlalchemy.exc import DBAPIError

from app.api.errors import error_response
from app.db.session import advisory_key
from app.db.tables import IdempotencyKeyRow
from app.errors import ErrorCode, IbxError
from app.logging import get_logger
from app.reliability.idempotency import _is_lock_timeout

log = get_logger("app.idempotency")

HEADER = "Idempotency-Key"
KEY_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,255}$")
PROTECTED_PREFIXES = ("/api/v1/", "/connector/v1/")
_REPLAY_HEADERS = ("content-type", "location")


def is_contract_call(path: str) -> bool:
    """Connector-contract methods (``/connector/v1/...`` and the ``/api/v1/connector/...`` mirror) skip this layer.

    * ``publish`` dedups on its body ``idempotency_key`` in the publish-intent ledger. A second, generated key
      here would give one call two different keys.
    * Reads (validate, get_status, fetch_metrics) must never replay a stale answer.
    * delete, refresh_auth and build_assist_pack are safe to repeat (a deleted post stays deleted; the pack's
      keys are deterministic).
    * This layer holds a DB connection for the whole request, and these routes call a platform: under load
      that exhausted the connection pool.
    """
    return path.startswith(("/connector/v1/", "/api/v1/connector/"))


def body_hash(body: bytes, content_type: str, query: str) -> str:
    """sha256 of the canonical body: JSON is re-serialised with sorted keys; multipart boundaries are ignored."""
    data = body
    if "application/json" in content_type and body.strip():
        try:
            data = json.dumps(json.loads(body), sort_keys=True, separators=(",", ":")).encode()
        except ValueError:
            pass
    elif "multipart/form-data" in content_type and "boundary=" in content_type:
        boundary = content_type.split("boundary=", 1)[1].split(";")[0].strip('"')
        data = body.replace(boundary.encode(), b"BOUNDARY")
    return hashlib.sha256(query.encode() + b"\n" + data).hexdigest()


class IdempotencyMiddleware:
    def __init__(self, app: Any, *, exclude: Callable[[str], bool] = is_contract_call) -> None:
        self.app = app
        self.exclude = exclude

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or scope["method"] != "POST" or not path.startswith(PROTECTED_PREFIXES) or self.exclude(path):
            await self.app(scope, receive, send)
            return
        state = scope["app"].state
        settings = state.settings
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        key = headers.get(HEADER.lower())
        extra_headers: dict[str, str] = {}
        if key is None:
            key = f"gen-{uuid.uuid4().hex}"
            extra_headers["X-IncuBrix-Warning"] = "IDEMPOTENCY_KEY_GENERATED"
            log.warning("idempotency_key_generated", path=path)
        elif not KEY_RE.match(key):
            err = IbxError(
                ErrorCode.REQUEST_VALIDATION_FAILED,
                "Idempotency-Key must be 1-255 characters of letters, digits and . _ : -",
                http_status=400,
            )
            await error_response(err)(scope, receive, send)
            return
        extra_headers[HEADER] = key
        engine = getattr(state, "engine", None)
        if engine is None or not settings.HTTP_IDEMPOTENCY_ENABLED:
            await self.app(scope, receive, _with_headers(send, extra_headers))
            return

        body = await _read_body(receive)
        req_hash = body_hash(body, headers.get("content-type", ""), scope.get("query_string", b"").decode("latin-1"))
        key_scope = f"POST {path}"
        now = state.clock.now()

        async with engine.connect() as conn:
            await conn.begin()
            try:
                await conn.execute(text(f"SET LOCAL lock_timeout = '{int(settings.IDEMPOTENCY_LOCK_WAIT_S * 1000)}ms'"))
                await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": advisory_key(f"http|{key_scope}|{key}")})
            except DBAPIError as exc:
                await conn.rollback()
                if not _is_lock_timeout(exc):
                    raise
                err = IbxError(
                    ErrorCode.IDEMPOTENCY_IN_PROGRESS,
                    "A request with this Idempotency-Key is still running; retry shortly.",
                    details={"idempotency_key": key},
                )
                await error_response(err, extra_headers)(scope, receive, send)
                return
            row = (
                await conn.execute(
                    select(IdempotencyKeyRow).where(IdempotencyKeyRow.scope == key_scope, IdempotencyKeyRow.key == key)
                )
            ).first()
            if row is not None and row.expires_at <= now:
                await conn.execute(delete(IdempotencyKeyRow).where(IdempotencyKeyRow.id == row.id))
                row = None
            if row is not None:
                await conn.rollback()
                if row.request_hash != req_hash:
                    err = IbxError(
                        ErrorCode.IDEMPOTENCY_KEY_REUSED,
                        "This Idempotency-Key was already used with a different request body.",
                        details={"idempotency_key": key},
                    )
                    await error_response(err, extra_headers)(scope, receive, send)
                    return
                replay = {**row.response_headers, **extra_headers, "Idempotent-Replayed": "true"}
                await _send_stored(send, row.response_status, row.response_body, list(replay.items()))
                return

            scope.setdefault("state", {})["idempotency_conn"] = conn
            captured = _Captured()
            try:
                await self.app(scope, _replay_body(body), captured.send)
            except BaseException:
                await conn.rollback()
                raise
            finally:
                scope["state"].pop("idempotency_conn", None)

            # An endpoint may keep a deliberate refusal (e.g. a blocked publication: 409 + its audit row) by setting
            # request.state.idempotency_keep_error; it is then committed and replayed like a success.
            keep = 200 <= captured.status < 300 or bool(scope["state"].pop("idempotency_keep_error", False))
            if keep:
                await conn.execute(
                    insert(IdempotencyKeyRow).values(
                        scope=key_scope,
                        key=key,
                        request_hash=req_hash,
                        job_id=_job_id(captured.body),
                        response_status=captured.status,
                        response_body=captured.body,
                        response_headers={k: v for k, v in captured.headers.items() if k in _REPLAY_HEADERS},
                        expires_at=now + timedelta(hours=settings.IDEMPOTENCY_TTL_H),
                    )
                )
                await conn.commit()
            else:
                await conn.rollback()
        live = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in captured.raw_headers]
        await _send_stored(send, captured.status, captured.body, live + list(extra_headers.items()))


class _Captured:
    """Collects the endpoint's response so nothing reaches the client before the commit."""

    def __init__(self) -> None:
        self.status = 500
        self.raw_headers: list[tuple[bytes, bytes]] = []
        self.chunks: list[bytes] = []

    async def send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.raw_headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            self.chunks.append(message.get("body", b""))

    @property
    def body(self) -> bytes:
        return b"".join(self.chunks)

    @property
    def headers(self) -> dict[str, str]:
        return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in self.raw_headers}


async def _read_body(receive: Any) -> bytes:
    chunks = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay_body(body: bytes) -> Callable[[], Any]:
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


def _with_headers(send: Any, extra: dict[str, str]) -> Any:
    async def wrapped(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            message["headers"] = list(message.get("headers", [])) + [(k.encode(), v.encode()) for k, v in extra.items()]
        await send(message)

    return wrapped


async def _send_stored(send: Any, status: int, body: bytes, headers: list[tuple[str, str]]) -> None:
    out = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers if k.lower() != "content-length"]
    out.append((b"content-length", str(len(body)).encode()))
    await send({"type": "http.response.start", "status": status, "headers": out})
    await send({"type": "http.response.body", "body": body})


def _job_id(body: bytes) -> str | None:
    try:
        data = json.loads(body)
    except ValueError:
        return None
    job_id = data.get("job_id") if isinstance(data, dict) else None
    return str(job_id)[:64] if job_id else None
