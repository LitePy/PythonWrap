"""Tests for python_wrap.web: idempotency middleware, rate-limit helpers, glue."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import types
from typing import Any

import pytest

from python_wrap.errors import BulkheadFull, CircuitOpenError, RateLimitExceeded
from python_wrap.idempotency import MemoryResultStore
from python_wrap.web import (
    IdempotencyMiddleware,
    WsgiIdempotencyMiddleware,
    django_response,
    fastapi_exception_handlers,
    flask_register_error_handlers,
    problem_response,
    ratelimit_headers,
    too_many_requests,
)

# ------------------------------------------------------------------ ASGI helpers


class Recorder:
    def __init__(self, status: int = 201, gate: asyncio.Event | None = None) -> None:
        self.status, self.gate, self.calls = status, gate, 0
        self.entered = asyncio.Event()
        self.bodies: list[bytes] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1
        self.entered.set()
        body = b""
        more = True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
        self.bodies.append(body)
        if self.gate is not None:
            await self.gate.wait()
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": [(b"content-type", b"text/plain"), (b"x-n", str(self.calls).encode())],
            }
        )
        await send({"type": "http.response.body", "body": b"part1-", "more_body": True})
        await send({"type": "http.response.body", "body": b"part2"})


async def call(
    app: Any,
    *,
    method: str = "POST",
    path: str = "/pay",
    key: str | None = "k1",
    body: bytes = b"{}",
    chunks: list[bytes] | None = None,
    extra: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    headers = list(extra or [])
    if key is not None:
        headers.append((b"idempotency-key", key.encode()))
    parts = chunks if chunks is not None else [body]
    if chunks is None:
        headers.append((b"content-length", str(len(body)).encode()))
    scope = {"type": "http", "method": method, "path": path, "headers": headers}
    queue = [
        {"type": "http.request", "body": p, "more_body": i < len(parts) - 1}
        for i, p in enumerate(parts)
    ]

    async def receive() -> dict[str, Any]:
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = sent[0]
    hdrs = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    return start["status"], hdrs, b"".join(m.get("body", b"") for m in sent[1:])


# ------------------------------------------------------------------ ASGI tests


async def test_asgi_replay_and_header() -> None:
    inner = Recorder()
    app = IdempotencyMiddleware(inner)
    first = await call(app)
    second = await call(app)
    assert first[0] == 201 and first[2] == b"part1-part2"
    assert "idempotent-replayed" not in first[1]
    assert second[0] == 201 and second[2] == b"part1-part2"
    assert second[1]["idempotent-replayed"] == "true"
    assert second[1]["x-n"] == "1"
    assert inner.calls == 1


async def test_asgi_fingerprint_mismatch_is_422() -> None:
    inner = Recorder()
    app = IdempotencyMiddleware(inner)
    await call(app, body=b'{"a":1}')
    status, headers, body = await call(app, body=b'{"a":2}')
    assert status == 422
    assert headers["content-type"] == "application/problem+json"
    assert json.loads(body)["status"] == 422
    assert inner.calls == 1


async def test_asgi_concurrent_same_key_gets_409() -> None:
    gate = asyncio.Event()
    inner = Recorder(gate=gate)
    app = IdempotencyMiddleware(inner)
    first = asyncio.create_task(call(app))
    await inner.entered.wait()
    status, headers, _ = await call(app)
    assert status == 409 and headers["retry-after"] == "1"
    gate.set()
    assert (await first)[0] == 201
    assert inner.calls == 1
    assert (await call(app))[1]["idempotent-replayed"] == "true"


async def test_asgi_5xx_not_stored_and_lease_released() -> None:
    inner = Recorder(status=503)
    app = IdempotencyMiddleware(inner)
    assert (await call(app))[0] == 503
    inner.status = 201
    status, headers, _ = await call(app)
    assert status == 201 and "idempotent-replayed" not in headers
    assert inner.calls == 2


async def test_asgi_4xx_is_stored() -> None:
    inner = Recorder(status=400)
    app = IdempotencyMiddleware(inner)
    await call(app)
    assert (await call(app))[1]["idempotent-replayed"] == "true"


async def test_asgi_exception_releases_lease() -> None:
    calls = 0

    async def boom(scope: Any, receive: Any, send: Any) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("bad")

    app = IdempotencyMiddleware(boom)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await call(app)
    assert calls == 2


async def test_asgi_required_header() -> None:
    inner = Recorder()
    app = IdempotencyMiddleware(inner, required=True)
    status, _, body = await call(app, key=None)
    assert status == 400 and json.loads(body)["status"] == 400
    assert (await call(app, method="GET", key=None))[0] == 201
    assert inner.calls == 1


async def test_asgi_body_limit() -> None:
    inner = Recorder()
    app = IdempotencyMiddleware(inner, max_body=10)
    assert (await call(app, body=b"x" * 11))[0] == 413
    assert (await call(app, key="k2", chunks=[b"x" * 6, b"x" * 6]))[0] == 413
    assert (await call(app, key="k3", body=b"x" * 10))[0] == 201
    assert inner.calls == 1


async def test_asgi_passthrough_without_header_or_method() -> None:
    inner = Recorder()
    app = IdempotencyMiddleware(inner)
    await call(app, key=None)
    await call(app, key=None)
    await call(app, method="GET")
    await call(app, method="GET")
    assert inner.calls == 4
    lifespan_calls: list[str] = []

    async def other(scope: Any, receive: Any, send: Any) -> None:
        lifespan_calls.append(scope["type"])

    await IdempotencyMiddleware(other)({"type": "lifespan"}, None, None)
    assert lifespan_calls == ["lifespan"]


async def test_asgi_scope_partitions_keys_and_chunked_body_forwarded() -> None:
    inner = Recorder()
    app = IdempotencyMiddleware(
        inner,
        scope=lambda s: dict(s["headers"]).get(b"x-user", b"").decode(),
    )
    await call(app, extra=[(b"x-user", b"alice")], chunks=[b"ab", b"cd"])
    await call(app, extra=[(b"x-user", b"bob")], chunks=[b"ab", b"cd"])
    assert inner.calls == 2 and inner.bodies == [b"abcd", b"abcd"]
    replay = await call(app, extra=[(b"x-user", b"alice")], chunks=[b"ab", b"cd"])
    assert replay[1]["idempotent-replayed"] == "true" and inner.calls == 2


async def test_asgi_invalid_key_and_shared_store_values_are_plain_data() -> None:
    store = MemoryResultStore()
    app = IdempotencyMiddleware(Recorder(), store=store)
    assert (await call(app, key="k" * 300))[0] == 400
    await call(app)
    (value,) = [v for v, _ in store._data.values()]
    assert json.loads(json.dumps(value)) == value
    assert set(value) == {"fp", "status", "headers", "body"}


def test_invalid_config() -> None:
    with pytest.raises(ValueError):
        IdempotencyMiddleware(Recorder(), max_body=-1)
    store = MemoryResultStore()
    store.fail_open = True  # type: ignore[attr-defined]
    with pytest.raises(ValueError):
        IdempotencyMiddleware(Recorder(), store=store)


# ------------------------------------------------------------------ WSGI tests


class WsgiApp:
    def __init__(self, status: str = "201 Created") -> None:
        self.status, self.calls, self.bodies = status, 0, []

    def __call__(self, environ: Any, start_response: Any) -> list[bytes]:
        self.calls += 1
        self.bodies.append(environ["wsgi.input"].read(int(environ["CONTENT_LENGTH"] or 0)))
        start_response(self.status, [("Content-Type", "text/plain"), ("X-N", str(self.calls))])
        return [b"ok-", b"body"]


def wcall(
    app: Any,
    *,
    method: str = "POST",
    key: str | None = "k1",
    body: bytes = b"{}",
    extra: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": "/pay",
        "SCRIPT_NAME": "",
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
        **(extra or {}),
    }
    if key is not None:
        environ["HTTP_IDEMPOTENCY_KEY"] = key
    seen: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> Any:
        seen["status"], seen["headers"] = status, headers

    payload = b"".join(app(environ, start_response))
    return int(seen["status"].split()[0]), {k.lower(): v for k, v in seen["headers"]}, payload


def test_wsgi_replay_mismatch_and_5xx() -> None:
    inner = WsgiApp()
    app = WsgiIdempotencyMiddleware(inner)
    first = wcall(app)
    second = wcall(app)
    assert first[2] == b"ok-body" and "idempotent-replayed" not in first[1]
    assert second[0] == 201 and second[1]["idempotent-replayed"] == "true"
    assert second[1]["x-n"] == "1" and inner.calls == 1
    assert wcall(app, body=b'{"z":1}')[0] == 422

    failing = WsgiApp("500 Internal Server Error")
    app2 = WsgiIdempotencyMiddleware(failing)
    wcall(app2)
    wcall(app2)
    assert failing.calls == 2


def test_wsgi_conflict_required_limit_passthrough() -> None:
    inner = WsgiApp()
    app = WsgiIdempotencyMiddleware(inner, required=True, max_body=5)
    assert wcall(app, key=None)[0] == 400
    assert wcall(app, body=b"x" * 6)[0] == 413
    assert wcall(app, method="GET", key=None)[0] == 201
    # in-flight lease held elsewhere -> 409
    core = app._core
    skey = core.storage_key({}, "POST", "/pay", "busy")
    assert core.backend.acquire(skey, "other", 60)
    status, headers, _ = wcall(app, key="busy", body=b"{}")
    assert status == 409 and headers["retry-after"] == "1"
    core.backend.release(skey, "other")
    assert wcall(app, key="busy", body=b"{}")[0] == 201
    assert inner.bodies[-1] == b"{}"


def test_wsgi_no_header_passthrough_and_scope() -> None:
    inner = WsgiApp()
    app = WsgiIdempotencyMiddleware(inner, required=False, scope=lambda e: e.get("USER", ""))
    wcall(app, key=None)
    wcall(app, key=None)
    assert inner.calls == 2
    wcall(app, extra={"USER": "a"})
    wcall(app, extra={"USER": "b"})
    assert inner.calls == 4


# ------------------------------------------------------------------ headers/problems


def test_ratelimit_headers() -> None:
    h = ratelimit_headers(limit=100, remaining=-3, reset=1.2, policy="100;w=60")
    assert h == {
        "RateLimit-Limit": "100",
        "RateLimit-Remaining": "0",
        "RateLimit-Reset": "2",
        "RateLimit-Policy": "100;w=60",
    }
    assert "RateLimit-Policy" not in ratelimit_headers(limit=1, remaining=1, reset=-5)
    assert ratelimit_headers(limit=1, remaining=1, reset=-5)["RateLimit-Reset"] == "0"


def test_too_many_requests() -> None:
    status, headers, body = too_many_requests(
        RateLimitExceeded("slow down", retry_after=2.1), limit=10
    )
    assert status == 429
    assert headers["Retry-After"] == "3" and headers["RateLimit-Remaining"] == "0"
    assert headers["RateLimit-Limit"] == "10" and headers["RateLimit-Reset"] == "3"
    doc = json.loads(body)
    assert doc["status"] == 429 and doc["detail"] == "slow down"
    status, headers, body = too_many_requests(0.2, detail="x")
    assert headers["Retry-After"] == "1" and "RateLimit-Limit" not in headers
    assert json.loads(body)["detail"] == "x"


def test_problem_response_mapping() -> None:
    assert problem_response(RateLimitExceeded("r", retry_after=1))[0] == 429
    status, headers, body = problem_response(CircuitOpenError("open", retry_after=4.5))
    assert status == 503 and headers["Retry-After"] == "5"
    assert headers["Content-Type"] == "application/problem+json"
    assert json.loads(body)["title"] == "Service Unavailable"
    status, headers, _ = problem_response(BulkheadFull("full", name="db", max_concurrent=2))
    assert status == 503 and headers["Retry-After"] == "1"
    with pytest.raises(TypeError):
        problem_response(ValueError("x"))


# ------------------------------------------------------------------ glue


def test_fastapi_handlers_with_fake_starlette(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __init__(self, content: bytes, status_code: int, headers: dict[str, str]) -> None:
            self.content, self.status_code, self.headers = content, status_code, headers

    pkg = types.ModuleType("starlette")
    mod = types.ModuleType("starlette.responses")
    mod.Response = Response  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "starlette", pkg)
    monkeypatch.setitem(sys.modules, "starlette.responses", mod)
    handlers = fastapi_exception_handlers()
    assert set(handlers) == {RateLimitExceeded, CircuitOpenError, BulkheadFull}
    resp = asyncio.run(handlers[RateLimitExceeded](None, RateLimitExceeded("x", retry_after=2)))
    assert resp.status_code == 429 and resp.headers["Retry-After"] == "2"


def test_flask_register_and_handler() -> None:
    registered: dict[type, Any] = {}

    class App:
        def register_error_handler(self, exc_type: type, fn: Any) -> None:
            registered[exc_type] = fn

    flask_register_error_handlers(App())
    assert set(registered) == {RateLimitExceeded, CircuitOpenError, BulkheadFull}
    body, status, headers = registered[CircuitOpenError](CircuitOpenError("o", retry_after=1))
    assert status == 503 and headers["Retry-After"] == "1" and json.loads(body)["status"] == 503


def test_django_response_with_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    class HttpResponse:
        def __init__(self, content: bytes, status: int, content_type: str) -> None:
            self.content, self.status_code, self.content_type = content, status, content_type
            self.headers: dict[str, str] = {}

        def __setitem__(self, key: str, value: str) -> None:
            self.headers[key] = value

    monkeypatch.setitem(sys.modules, "django", types.ModuleType("django"))
    mod = types.ModuleType("django.http")
    mod.HttpResponse = HttpResponse  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "django.http", mod)
    resp = django_response(RateLimitExceeded("x", retry_after=7))
    assert resp.status_code == 429 and resp.content_type == "application/problem+json"
    assert resp.headers == {"Retry-After": "7"}
