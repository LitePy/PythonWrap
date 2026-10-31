"""Server-side web helpers: Idempotency-Key middleware and rate-limit responses.

* `IdempotencyMiddleware` (ASGI) and `WsgiIdempotencyMiddleware` (WSGI) make unsafe
  requests such as POST safe to retry, following the Stripe convention and the
  IETF ``Idempotency-Key`` header draft. Neither imports a web framework.
* `ratelimit_headers`, `too_many_requests` and `problem_response` build
  ``RateLimit-*`` / ``Retry-After`` headers and RFC 9457 ``application/problem+json``
  bodies from this package's exceptions; small lazy glue functions plug them into
  FastAPI/Starlette, Flask and Django.

Idempotency keys MUST be scoped by authenticated identity (see the ``scope`` argument);
otherwise two users who happen to send the same key would share responses.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from python_wrap._runtime import seconds
from python_wrap.coordination import LocalLease
from python_wrap.errors import BulkheadFull, CircuitOpenError, RateLimitExceeded
from python_wrap.idempotency import MemoryResultStore

__all__ = [
    "IdempotencyMiddleware",
    "WsgiIdempotencyMiddleware",
    "django_response",
    "fastapi_exception_handlers",
    "flask_register_error_handlers",
    "problem_response",
    "ratelimit_headers",
    "too_many_requests",
]

_PROBLEM = "application/problem+json"
_MAX_KEY_LENGTH = 255
_PREFIX = "python-wrap:http-idem:"


# --------------------------------------------------------------------------- problems


def _problem_body(status: int, title: str, detail: str | None, type_: str = "about:blank") -> bytes:
    doc: dict[str, Any] = {"type": type_, "title": title, "status": status}
    if detail:
        doc["detail"] = detail
    return json.dumps(doc).encode("utf-8")


def _problem(
    status: int, title: str, detail: str | None = None, extra: dict[str, str] | None = None
) -> tuple[int, list[tuple[str, str]], bytes]:
    body = _problem_body(status, title, detail)
    headers = [("Content-Type", _PROBLEM), ("Content-Length", str(len(body)))]
    headers.extend((extra or {}).items())
    return status, headers, body


# --------------------------------------------------------------------------- shared core


class _Core:
    """Request-independent idempotency logic shared by the ASGI and WSGI wrappers."""

    def __init__(
        self,
        *,
        store: Any,
        backend: Any,
        ttl: float | str,
        lease_ttl: float | str,
        methods: Iterable[str],
        header: str,
        required: bool,
        scope: Callable[[Any], str] | None,
        max_body: int,
    ) -> None:
        self.ttl, self.lease_ttl = seconds(ttl), seconds(lease_ttl)
        if isinstance(max_body, bool) or not isinstance(max_body, int) or max_body < 0:
            raise ValueError("max_body must be a non-negative integer")
        if getattr(store, "fail_open", False):
            raise ValueError("idempotency requires a result store with fail_open=False")
        self.store = MemoryResultStore() if store is None else store
        self.backend = LocalLease() if backend is None else backend
        self.methods = frozenset(m.upper() for m in methods)
        self.header, self.required = header, required
        self.scope, self.max_body = scope, max_body

    def storage_key(self, source: Any, method: str, path: str, key: str) -> str:
        who = self.scope(source) if self.scope is not None else ""
        raw = "\0".join((str(who), method, path, key))
        return _PREFIX + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def fingerprint(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def check_key(self, key: str | None) -> tuple[int, list[tuple[str, str]], bytes] | None:
        if key is None:
            if self.required:
                return _problem(400, "Idempotency-Key header is required")
            return None
        if not key.strip() or len(key) > _MAX_KEY_LENGTH:
            return _problem(400, "Invalid Idempotency-Key header")
        return None

    @staticmethod
    def too_large() -> tuple[int, list[tuple[str, str]], bytes]:
        return _problem(413, "Request body too large")

    def lookup(
        self, skey: str, fp: str
    ) -> tuple[int, list[tuple[str, str]], bytes] | dict[str, Any] | None:
        """Return a stored record to replay, a 422 problem, or None when unseen."""
        found = self.store.get(skey)
        if found is None:
            return None
        if found.get("fp") != fp:
            return _problem(
                422,
                "Idempotency-Key reused with a different request",
                "this key was already used with a different request body",
            )
        return found  # type: ignore[no-any-return]

    @staticmethod
    def conflict() -> tuple[int, list[tuple[str, str]], bytes]:
        return _problem(
            409,
            "A request with this Idempotency-Key is already in progress",
            None,
            {"Retry-After": "1"},
        )

    def record(self, fp: str, status: int, headers: list[tuple[str, str]], body: bytes) -> Any:
        return {
            "fp": fp,
            "status": status,
            "headers": [[k, v] for k, v in headers],
            "body": body.decode("latin-1"),
        }

    def should_store(self, status: int, size: int) -> bool:
        return status < 500 and size <= self.max_body

    def save(self, skey: str, value: Any) -> None:
        self.store.set(skey, value, self.ttl)

    @staticmethod
    def replay_parts(found: dict[str, Any]) -> tuple[int, list[tuple[str, str]], bytes]:
        headers = [
            (str(k), str(v))
            for k, v in found["headers"]
            if str(k).lower() not in {"idempotent-replayed"}
        ]
        headers.append(("Idempotent-Replayed", "true"))
        return int(found["status"]), headers, str(found["body"]).encode("latin-1")


# --------------------------------------------------------------------------- ASGI


class IdempotencyMiddleware:
    """Pure ASGI middleware giving unsafe HTTP methods Idempotency-Key semantics.

    For a request with the header and a matching method the response of the first
    execution is stored and replayed (with ``Idempotent-Replayed: true``) for later
    requests with the same key and the same body. The same key with a different body
    gets 422; a concurrent in-flight duplicate gets 409 with ``Retry-After: 1``.
    Responses with status >= 500 are not stored so clients can retry. Requests without
    the header stream through untouched.

    Args:
        app: The wrapped ASGI application.
        store: Result store with ``get/set(key, value, ttl)/delete``; values are plain
            JSON-compatible dicts. Defaults to an in-process `MemoryResultStore`.
        backend: Lease backend (``acquire/release``); defaults to a `LocalLease`. Use a
            shared one (for example `RedisLock`) together with a shared store when
            running several processes.
        ttl: How long responses are retained.
        lease_ttl: In-flight lease lifetime; must exceed the slowest handler.
        methods: HTTP methods the header applies to.
        header: Request header carrying the key.
        required: Reject matching requests without the header with 400.
        scope: ``callable(asgi_scope) -> str`` partitioning keys, e.g. by tenant/user.
            Keys must be scoped by authenticated identity.
        max_body: Largest request body buffered for fingerprinting; larger gets 413.

    Example:
        >>> async def app(scope, receive, send): ...
        >>> wrapped = IdempotencyMiddleware(app, ttl="1h")
    """

    def __init__(
        self,
        app: Any,
        *,
        store: Any = None,
        backend: Any = None,
        ttl: float | str = "1d",
        lease_ttl: float | str = 60,
        methods: Iterable[str] = ("POST", "PATCH"),
        header: str = "Idempotency-Key",
        required: bool = False,
        scope: Callable[[Any], str] | None = None,
        max_body: int = 1_000_000,
    ) -> None:
        self.app = app
        self._core = _Core(
            store=store,
            backend=backend,
            ttl=ttl,
            lease_ttl=lease_ttl,
            methods=methods,
            header=header,
            required=required,
            scope=scope,
            max_body=max_body,
        )
        self._header = header.lower().encode("latin-1")

    async def _respond(
        self,
        send: Callable[[dict[str, Any]], Awaitable[None]],
        parts: tuple[int, list[tuple[str, str]], bytes],
    ) -> None:
        status, headers, body = parts
        out = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers]
        out = [(k, v) for k, v in out if k != b"content-length"]
        out.append((b"content-length", str(len(body)).encode()))
        await send({"type": "http.response.start", "status": status, "headers": out})
        await send({"type": "http.response.body", "body": body})

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        core = self._core
        method = str(scope.get("method", "")).upper()
        if scope.get("type") != "http" or method not in core.methods:
            await self.app(scope, receive, send)
            return
        raw_headers = scope.get("headers", [])
        key: str | None = None
        length: int | None = None
        for name, value in raw_headers:
            if name.lower() == self._header:
                key = value.decode("latin-1")
            elif name.lower() == b"content-length":
                try:
                    length = int(value)
                except ValueError:
                    length = None
        problem = core.check_key(key)
        if problem is not None:
            await self._respond(send, problem)
            return
        if key is None:
            await self.app(scope, receive, send)
            return
        if length is not None and length > core.max_body:
            await self._respond(send, core.too_large())
            return

        chunks: list[bytes] = []
        size = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            part = message.get("body", b"")
            size += len(part)
            if size > core.max_body:
                await self._respond(send, core.too_large())
                return
            chunks.append(part)
            more = bool(message.get("more_body", False))
        body = b"".join(chunks)
        fp = core.fingerprint(body)
        skey = core.storage_key(scope, method, str(scope.get("path", "")), key)

        async def run(fn: Callable[..., Any], *args: Any) -> Any:
            return await asyncio.to_thread(fn, *args)

        seen = await run(core.lookup, skey, fp)
        if seen is not None:
            parts = seen if isinstance(seen, tuple) else core.replay_parts(seen)
            await self._respond(send, parts)
            return
        token = uuid.uuid4().hex
        if not await run(core.backend.acquire, skey, token, core.lease_ttl):
            await self._respond(send, core.conflict())
            return
        try:
            seen = await run(core.lookup, skey, fp)
            if seen is not None:
                parts = seen if isinstance(seen, tuple) else core.replay_parts(seen)
                await self._respond(send, parts)
                return
            replayed = False

            async def replay_receive() -> dict[str, Any]:
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            status = 0
            headers: list[tuple[str, str]] = []
            captured: list[bytes] = []
            captured_size = 0

            async def capture_send(message: dict[str, Any]) -> None:
                nonlocal status, headers, captured_size
                if message["type"] == "http.response.start":
                    status = int(message["status"])
                    headers = [
                        (k.decode("latin-1"), v.decode("latin-1"))
                        for k, v in message.get("headers", [])
                    ]
                elif message["type"] == "http.response.body":
                    part = bytes(message.get("body", b""))
                    captured_size += len(part)
                    if captured_size <= core.max_body:
                        captured.append(part)
                await send(message)

            await self.app(scope, replay_receive, capture_send)
            if status and core.should_store(status, captured_size):
                value = core.record(fp, status, headers, b"".join(captured))
                await run(core.save, skey, value)
        finally:
            await asyncio.shield(run(core.backend.release, skey, token))


# --------------------------------------------------------------------------- WSGI


class WsgiIdempotencyMiddleware:
    """WSGI counterpart of `IdempotencyMiddleware` with identical semantics.

    Works with Flask and Django's WSGI entry point. Responses of idempotent requests are
    buffered (they are stored anyway); requests without the header are untouched.

    Args:
        app: The wrapped WSGI application.
        store: Result store; defaults to an in-process `MemoryResultStore`.
        backend: Lease backend; defaults to a `LocalLease`.
        ttl: How long responses are retained.
        lease_ttl: In-flight lease lifetime; must exceed the slowest handler.
        methods: HTTP methods the header applies to.
        header: Request header carrying the key.
        required: Reject matching requests without the header with 400.
        scope: ``callable(environ) -> str`` partitioning keys by tenant/user.
        max_body: Largest request body buffered; larger gets 413.

    Example:
        >>> def app(environ, start_response): ...
        >>> wrapped = WsgiIdempotencyMiddleware(app)
    """

    def __init__(
        self,
        app: Any,
        *,
        store: Any = None,
        backend: Any = None,
        ttl: float | str = "1d",
        lease_ttl: float | str = 60,
        methods: Iterable[str] = ("POST", "PATCH"),
        header: str = "Idempotency-Key",
        required: bool = False,
        scope: Callable[[Any], str] | None = None,
        max_body: int = 1_000_000,
    ) -> None:
        self.app = app
        self._core = _Core(
            store=store,
            backend=backend,
            ttl=ttl,
            lease_ttl=lease_ttl,
            methods=methods,
            header=header,
            required=required,
            scope=scope,
            max_body=max_body,
        )
        self._environ_key = "HTTP_" + header.upper().replace("-", "_")

    @staticmethod
    def _respond(
        start_response: Callable[..., Any], parts: tuple[int, list[tuple[str, str]], bytes]
    ) -> list[bytes]:
        status, headers, body = parts
        from http import HTTPStatus

        try:
            phrase = HTTPStatus(status).phrase
        except ValueError:
            phrase = "Unknown"
        out = [(k, v) for k, v in headers if k.lower() != "content-length"]
        out.append(("Content-Length", str(len(body))))
        start_response(f"{status} {phrase}", out)
        return [body]

    def __call__(
        self, environ: dict[str, Any], start_response: Callable[..., Any]
    ) -> Iterable[bytes]:
        core = self._core
        method = str(environ.get("REQUEST_METHOD", "")).upper()
        if method not in core.methods:
            return self.app(environ, start_response)  # type: ignore[no-any-return]
        key = environ.get(self._environ_key)
        key = None if key is None else str(key)
        problem = core.check_key(key)
        if problem is not None:
            return self._respond(start_response, problem)
        if key is None:
            return self.app(environ, start_response)  # type: ignore[no-any-return]
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > core.max_body:
            return self._respond(start_response, core.too_large())
        stream = environ.get("wsgi.input")
        body = stream.read(length) if stream is not None and length > 0 else b""
        fp = core.fingerprint(body)
        path = str(environ.get("SCRIPT_NAME", "")) + str(environ.get("PATH_INFO", ""))
        skey = core.storage_key(environ, method, path, key)

        seen = core.lookup(skey, fp)
        if seen is not None:
            parts = seen if isinstance(seen, tuple) else core.replay_parts(seen)
            return self._respond(start_response, parts)
        token = uuid.uuid4().hex
        if not core.backend.acquire(skey, token, core.lease_ttl):
            return self._respond(start_response, core.conflict())
        try:
            seen = core.lookup(skey, fp)
            if seen is not None:
                parts = seen if isinstance(seen, tuple) else core.replay_parts(seen)
                return self._respond(start_response, parts)
            environ["wsgi.input"] = io.BytesIO(body)
            environ["CONTENT_LENGTH"] = str(len(body))
            state: dict[str, Any] = {"status": "", "headers": []}
            written: list[bytes] = []

            def capture_start(
                status: str, headers: list[tuple[str, str]], exc_info: Any = None
            ) -> Callable[[bytes], None]:
                state["status"], state["headers"] = status, list(headers)
                return written.append

            result = self.app(environ, capture_start)
            chunks = list(written)
            try:
                chunks.extend(result)
            finally:
                close = getattr(result, "close", None)
                if close is not None:
                    close()
            payload = b"".join(chunks)
            code = int(str(state["status"]).split(" ", 1)[0])
            if core.should_store(code, len(payload)):
                core.save(skey, core.record(fp, code, state["headers"], payload))
            start_response(state["status"], state["headers"])
            return [payload]
        finally:
            core.backend.release(skey, token)


# --------------------------------------------------------------------------- rate limits


def ratelimit_headers(
    *, limit: int, remaining: int, reset: float, policy: str | None = None
) -> dict[str, str]:
    """Build IETF ``RateLimit-*`` response headers.

    Args:
        limit: Requests allowed in the window.
        remaining: Requests left (clamped to >= 0).
        reset: Seconds until the window resets (rounded up, clamped to >= 0).
        policy: Optional ``RateLimit-Policy`` value such as ``"100;w=60"``.

    Returns:
        A header dict.

    Example:
        >>> ratelimit_headers(limit=100, remaining=7, reset=1.2)["RateLimit-Reset"]
        '2'
    """
    headers = {
        "RateLimit-Limit": str(int(limit)),
        "RateLimit-Remaining": str(max(0, int(remaining))),
        "RateLimit-Reset": str(max(0, math.ceil(reset))),
    }
    if policy is not None:
        headers["RateLimit-Policy"] = policy
    return headers


def _retry_seconds(value: float) -> int:
    return max(1, math.ceil(value))


def too_many_requests(
    exc_or_retry_after: RateLimitExceeded | float,
    *,
    limit: int | None = None,
    detail: str | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Build a 429 response: ``Retry-After``, optional ``RateLimit-*`` and problem+json.

    Args:
        exc_or_retry_after: A `RateLimitExceeded` or a retry delay in seconds.
        limit: When given, ``RateLimit-Limit/Remaining/Reset`` headers are included.
        detail: Problem detail text; defaults to the exception message.

    Returns:
        ``(status, headers, body)``.

    Example:
        >>> too_many_requests(2.5)[0]
        429
    """
    retry = float(
        exc_or_retry_after.retry_after
        if isinstance(exc_or_retry_after, RateLimitExceeded)
        else exc_or_retry_after
    )
    if detail is None and isinstance(exc_or_retry_after, Exception):
        detail = str(exc_or_retry_after) or None
    wait = _retry_seconds(retry)
    headers = {"Content-Type": _PROBLEM, "Retry-After": str(wait)}
    if limit is not None:
        headers.update(ratelimit_headers(limit=limit, remaining=0, reset=retry))
    return 429, headers, _problem_body(429, "Too Many Requests", detail)


def problem_response(exc: BaseException) -> tuple[int, dict[str, str], bytes]:
    """Map a resilience exception to ``(status, headers, problem+json body)``.

    `RateLimitExceeded` gives 429, `CircuitOpenError` 503 with ``Retry-After`` from the
    breaker, and `BulkheadFull` 503 with ``Retry-After: 1``.

    Raises:
        TypeError: For any other exception type.

    Example:
        >>> problem_response(BulkheadFull("full", name=None, max_concurrent=1))[0]
        503
    """
    if isinstance(exc, RateLimitExceeded):
        return too_many_requests(exc)
    if isinstance(exc, CircuitOpenError):
        wait = _retry_seconds(exc.retry_after)
        title = "Service Unavailable"
    elif isinstance(exc, BulkheadFull):
        wait, title = 1, "Service Unavailable"
    else:
        raise TypeError(f"no problem response for {type(exc).__name__}")
    headers = {"Content-Type": _PROBLEM, "Retry-After": str(wait)}
    return 503, headers, _problem_body(503, title, str(exc) or None)


_HANDLED: tuple[type[BaseException], ...] = (RateLimitExceeded, CircuitOpenError, BulkheadFull)


def fastapi_exception_handlers() -> dict[type[BaseException], Callable[..., Any]]:
    """Return ``{exception_type: async handler}`` for FastAPI/Starlette.

    Register with ``app.add_exception_handler(type, handler)`` for each pair, or pass the
    dict as ``FastAPI(exception_handlers=...)``. Imports ``starlette.responses`` lazily.

    Example:
        >>> # for t, h in fastapi_exception_handlers().items(): app.add_exception_handler(t, h)
    """
    import importlib

    response_class = importlib.import_module("starlette.responses").Response

    async def handler(request: Any, exc: BaseException) -> Any:
        status, headers, body = problem_response(exc)
        return response_class(content=body, status_code=status, headers=headers)

    return dict.fromkeys(_HANDLED, handler)


def flask_register_error_handlers(app: Any) -> None:
    """Register problem+json handlers on a Flask app (``app.register_error_handler``).

    Example:
        >>> # flask_register_error_handlers(app)
    """

    def handler(exc: BaseException) -> tuple[bytes, int, dict[str, str]]:
        status, headers, body = problem_response(exc)
        return body, status, headers

    for exc_type in _HANDLED:
        app.register_error_handler(exc_type, handler)


def django_response(exc: BaseException) -> Any:
    """Build a Django ``HttpResponse`` for a handled exception (lazy ``django.http``).

    Example:
        >>> # return django_response(exc)  # inside a view or middleware
    """
    import importlib

    status, headers, body = problem_response(exc)
    http = importlib.import_module("django.http")
    response = http.HttpResponse(body, status=status, content_type=_PROBLEM)
    for name, value in headers.items():
        if name != "Content-Type":
            response[name] = value
    return response
