"""Regression tests for the issues fixed by the security review (see CHANGELOG, Security)."""

from __future__ import annotations

import asyncio
import io
import json
import math
from typing import Any

import pytest

from python_wrap import BulkheadFull, CircuitOpenError, retry, retry_after_header
from python_wrap.idempotency import MemoryResultStore
from python_wrap.retry import HINT_CEILING
from python_wrap.web import IdempotencyMiddleware, WsgiIdempotencyMiddleware, problem_response


class CookieApp:
    """Sets a per-request session cookie and echoes the caller's Authorization."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.calls += 1
        while (await receive()).get("more_body"):
            pass
        auth = dict(scope["headers"]).get(b"authorization", b"anonymous")
        await send(
            {
                "type": "http.response.start",
                "status": 201,
                "headers": [(b"set-cookie", b"session=" + auth), (b"x-ok", b"1")],
            }
        )
        await send({"type": "http.response.body", "body": b"owner=" + auth})


async def asgi(
    app: Any, *, auth: bytes | None = None, query: bytes = b"", body: bytes = b"{}"
) -> tuple[int, dict[bytes, bytes], bytes]:
    headers = [(b"idempotency-key", b"same-key"), (b"content-length", str(len(body)).encode())]
    if auth is not None:
        headers.append((b"authorization", auth))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/orders",
        "query_string": query,
        "headers": headers,
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), out


def test_replayed_responses_never_carry_the_first_clients_cookies():
    inner = CookieApp()
    app = IdempotencyMiddleware(inner)
    first = asyncio.run(asgi(app, auth=b"token-a"))
    replay = asyncio.run(asgi(app, auth=b"token-a"))
    assert first[1][b"set-cookie"] == b"session=token-a"
    assert inner.calls == 1
    assert b"set-cookie" not in {k.lower() for k in replay[1]}
    assert replay[1][b"idempotent-replayed"] == b"true"


def test_same_key_from_different_credentials_is_not_shared_by_default():
    inner = CookieApp()
    app = IdempotencyMiddleware(inner)
    first = asyncio.run(asgi(app, auth=b"token-a"))
    other = asyncio.run(asgi(app, auth=b"token-b"))
    assert first[2] == b"owner=token-a"
    assert other[2] == b"owner=token-b"  # executed for the second client, not replayed
    assert inner.calls == 2


def test_same_key_with_a_different_query_string_is_a_mismatch():
    inner = CookieApp()
    app = IdempotencyMiddleware(inner)
    assert asyncio.run(asgi(app, query=b"amount=1"))[0] == 201
    status, _, _ = asyncio.run(asgi(app, query=b"amount=1000"))
    assert status == 422
    assert inner.calls == 1


def wsgi_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
    start_response("201 Created", [("Content-Type", "text/plain")])
    return [b"ok"]


def test_wsgi_chunked_body_without_length_is_bounded():
    app = WsgiIdempotencyMiddleware(wsgi_app, max_body=10)
    seen: dict[str, str] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        seen["status"] = status

    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/orders",
        "HTTP_IDEMPOTENCY_KEY": "k",
        "HTTP_TRANSFER_ENCODING": "chunked",
        "wsgi.input": io.BytesIO(b"x" * 1000),
        "wsgi.input_terminated": True,
    }
    b"".join(app(environ, start_response))
    assert seen["status"].startswith("413")


def test_wsgi_chunked_bodies_are_fingerprinted():
    app = WsgiIdempotencyMiddleware(wsgi_app)
    statuses = []

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        statuses.append(status)

    for body in (b"amount=1", b"amount=9"):
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/orders",
            "HTTP_IDEMPOTENCY_KEY": "k",
            "wsgi.input": io.BytesIO(body),
            "wsgi.input_terminated": True,
        }
        b"".join(app(environ, start_response))
    assert statuses[0].startswith("201") and statuses[1].startswith("422")


@pytest.mark.parametrize(
    "exc",
    [
        CircuitOpenError("circuit internal-ledger-db is open", retry_after=5),
        BulkheadFull("bulkhead payments-pool is full", name="payments-pool", max_concurrent=3),
    ],
)
def test_problem_responses_do_not_disclose_internal_names(exc):
    status, headers, body = problem_response(exc)
    assert status == 503 and "Retry-After" in headers
    text = body.decode()
    assert "internal-ledger-db" not in text and "payments-pool" not in text
    assert json.loads(text)["title"] == "Service Unavailable"


def test_memory_result_store_is_bounded():
    store = MemoryResultStore(max_entries=100)
    for index in range(10_000):
        store.set(f"client-key-{index}", {"status": 200}, 3600)
    assert len(store) == 100
    assert store.get("client-key-9999") == {"status": 200}
    with pytest.raises(ValueError):
        MemoryResultStore(max_entries=0)


class Throttled(Exception):
    def __init__(self, retry_after: object) -> None:
        super().__init__("throttled")
        self.retry_after = retry_after


@pytest.mark.parametrize("hint", [10**9, "Fri, 31 Dec 9999 23:59:59 GMT"])
def test_huge_retry_after_cannot_park_a_worker(fake_clock, hint):
    attempts = []

    @retry(max_attempts=2, on=Throttled, delay_from=retry_after_header)
    def call() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise Throttled(hint)
        return "ok"

    assert call() == "ok"
    assert fake_clock.sleeps == [HINT_CEILING]


def test_explicit_max_delay_still_wins_and_infinite_hints_are_ignored(fake_clock):
    attempts = []

    @retry(max_attempts=3, delay=2, max_delay=30, on=Throttled, delay_from=lambda e: e.retry_after)
    def call() -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise Throttled(10**6)
        if len(attempts) == 2:
            raise Throttled(math.inf)
        return "ok"

    assert call() == "ok"
    assert fake_clock.sleeps == [30, 2]


def test_slack_alerts_cannot_mention_channels_or_inject_links():
    from python_wrap.alerts import WebhookAlerter

    sent: list[bytes] = []
    alerter = WebhookAlerter(
        "https://hooks.example.test/x",
        format="slack",
        sender=lambda url, body, headers: sent.append(body),
        context={"env": "<!channel> <https://evil.example|click>"},
    )
    breaker_name = "orders <!here>"
    assert alerter.notify(breaker_name, "closed", "open")
    alerter.flush(5)
    text = json.loads(sent[0])["text"]
    assert "<!channel>" not in text and "<!here>" not in text and "<https" not in text
    assert "&lt;!channel&gt;" in text
