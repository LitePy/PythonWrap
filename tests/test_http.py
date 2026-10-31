"""Tests for python_wrap.http."""

from __future__ import annotations

import sys
import types
from typing import Any, ClassVar

import pytest

from python_wrap.deadline import deadline
from python_wrap.events import listening
from python_wrap.http import (
    IDEMPOTENT_METHODS,
    RETRYABLE_STATUSES,
    AsyncRetryTransport,
    HttpRetryPolicy,
    RetryTransport,
    http_retry,
    mount_retries,
    retry_delay,
    should_retry,
)
from python_wrap.retry import RetryBudget
from python_wrap.testing import FakeClock


class Url:
    host = "api.example.com"


class Request:
    def __init__(self, method: str = "GET", headers: dict[str, str] | None = None) -> None:
        self.method = method
        self.headers = headers or {}
        self.url = Url()


class Response:
    def __init__(self, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True


class Inner:
    """Plays a script of responses/exceptions."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.calls = 0
        self.closed = False

    def _next(self) -> Any:
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def handle_request(self, request: Any) -> Any:
        return self._next()

    async def handle_async_request(self, request: Any) -> Any:
        return self._next()

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True


class ReadTimeout(Exception):
    """Looks like httpx.ReadTimeout by name only."""


class TransportError(Exception):
    pass


class ConnectError(TransportError):
    pass


# ---------------------------------------------------------------- decision table


@pytest.mark.parametrize("method", sorted(IDEMPOTENT_METHODS))
def test_idempotent_methods_retry_on_status(method: str) -> None:
    assert should_retry(method, {}, status=503)
    assert should_retry(method.lower(), {}, exc=ConnectionError())


@pytest.mark.parametrize("method", ["POST", "PATCH", "post"])
def test_non_idempotent_never_retried_without_key(method: str) -> None:
    assert not should_retry(method, {}, status=503)
    assert not should_retry(method, {}, exc=ConnectionError())


def test_idempotency_key_enables_post_case_insensitive() -> None:
    assert should_retry("POST", {"idempotency-key": "k"}, status=503)
    assert should_retry("PATCH", {"Idempotency-Key": "k"}, exc=TimeoutError())
    off = HttpRetryPolicy(idempotency_header=None)
    assert not should_retry("POST", {"Idempotency-Key": "k"}, status=503, policy=off)
    custom = HttpRetryPolicy(idempotency_header="X-Req-Id")
    assert should_retry("POST", {"X-Req-Id": "1"}, status=503, policy=custom)


@pytest.mark.parametrize("status", sorted(RETRYABLE_STATUSES))
def test_retryable_statuses(status: int) -> None:
    assert should_retry("GET", {}, status=status)


@pytest.mark.parametrize("status", [200, 301, 400, 401, 404, 501])
def test_other_statuses_not_retried(status: int) -> None:
    assert not should_retry("GET", {}, status=status)


def test_nothing_to_judge_is_false() -> None:
    assert not should_retry("GET", {})


def test_exception_matching() -> None:
    assert should_retry("GET", {}, exc=ConnectionResetError())
    assert should_retry("GET", {}, exc=TimeoutError())
    assert should_retry("GET", {}, exc=ReadTimeout())  # by name
    assert should_retry("GET", {}, exc=ConnectError())  # parent named TransportError
    assert not should_retry("GET", {}, exc=ValueError())


def test_policy_validation() -> None:
    with pytest.raises(ValueError):
        HttpRetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        HttpRetryPolicy(backoff=0.5)
    with pytest.raises(TypeError):
        HttpRetryPolicy(budget=object())  # type: ignore[arg-type]
    assert HttpRetryPolicy(methods=frozenset({"get"})).methods == frozenset({"GET"})


# ---------------------------------------------------------------- delays


def test_retry_after_seconds_and_cap() -> None:
    policy = HttpRetryPolicy(max_retry_after=10)
    assert retry_delay(Response(429, {"Retry-After": "3"}), 1, policy) == 3.0
    assert retry_delay(Response(429, {"Retry-After": "11"}), 1, policy) is None


def test_retry_after_http_date() -> None:
    import datetime
    from email.utils import format_datetime

    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30)
    wait = retry_delay(Response(503, {"Retry-After": format_datetime(when, usegmt=True)}), 1)
    assert wait is not None and 25 < wait <= 30


def test_retry_after_ignored_when_disabled() -> None:
    policy = HttpRetryPolicy(respect_retry_after=False, delay=1)
    assert retry_delay(Response(429, {"Retry-After": "99"}), 1, policy) == 1.0


def test_backoff_cap_and_jitter() -> None:
    policy = HttpRetryPolicy(delay=1, backoff=2, max_delay=5)
    assert [retry_delay(Response(503), n, policy) for n in (1, 2, 3, 4)] == [1, 2, 4, 5]
    jittery = HttpRetryPolicy(delay=1, jitter=0.5)
    assert 1.0 <= (retry_delay(Response(503), 1, jittery) or 0) <= 1.5


# ---------------------------------------------------------------- sync transport


def test_sync_retries_status_and_closes_discarded(fake_clock: FakeClock) -> None:
    bad1, bad2, good = Response(503), Response(502), Response(200)
    inner = Inner(bad1, bad2, good)
    transport = RetryTransport(inner, HttpRetryPolicy(delay=1, backoff=2))
    assert transport.handle_request(Request()) is good
    assert fake_clock.sleeps == [1, 2]
    assert bad1.closed and bad2.closed and not good.closed


def test_sync_returns_last_response_when_exhausted(fake_clock: FakeClock) -> None:
    responses = [Response(503) for _ in range(3)]
    transport = RetryTransport(Inner(*responses), HttpRetryPolicy(delay=1))
    assert transport.handle_request(Request()) is responses[-1]
    assert fake_clock.sleeps == [1, 2]
    assert not responses[-1].closed


def test_sync_retries_exception_then_raises(fake_clock: FakeClock) -> None:
    inner = Inner(ConnectionError("a"), ConnectionError("b"), ConnectionError("c"))
    transport = RetryTransport(inner, HttpRetryPolicy(delay=1))
    with pytest.raises(ConnectionError, match="c"):
        transport.handle_request(Request())
    assert inner.calls == 3
    assert fake_clock.sleeps == [1, 2]


def test_sync_post_not_retried(fake_clock: FakeClock) -> None:
    inner = Inner(Response(503))
    result = RetryTransport(inner).handle_request(Request("POST"))
    assert result.status_code == 503 and inner.calls == 1
    inner = Inner(ConnectionError("x"))
    with pytest.raises(ConnectionError):
        RetryTransport(inner).handle_request(Request("POST"))
    assert inner.calls == 1 and fake_clock.sleeps == []


def test_sync_post_with_key_retried(fake_clock: FakeClock) -> None:
    inner = Inner(Response(503), Response(201))
    out = RetryTransport(inner).handle_request(Request("POST", {"Idempotency-Key": "k"}))
    assert out.status_code == 201 and inner.calls == 2


def test_sync_non_transport_exception_propagates(fake_clock: FakeClock) -> None:
    inner = Inner(ValueError("bug"))
    with pytest.raises(ValueError):
        RetryTransport(inner).handle_request(Request())
    assert inner.calls == 1


def test_sync_retry_after_used_exactly(fake_clock: FakeClock) -> None:
    inner = Inner(Response(429, {"Retry-After": "7"}), Response(200))
    RetryTransport(inner, HttpRetryPolicy(delay=1)).handle_request(Request())
    assert fake_clock.sleeps == [7]


def test_sync_retry_after_too_long_returns_response(fake_clock: FakeClock) -> None:
    resp = Response(429, {"Retry-After": "600"})
    inner = Inner(resp)
    assert RetryTransport(inner).handle_request(Request()) is resp
    assert inner.calls == 1 and fake_clock.sleeps == []


def test_sync_budget_refusal_stops_retries(fake_clock: FakeClock) -> None:
    budget = RetryBudget(ratio=0, min_per_second=0)
    resp = Response(503)
    inner = Inner(resp)
    transport = RetryTransport(inner, HttpRetryPolicy(budget=budget))
    assert transport.handle_request(Request()) is resp
    assert inner.calls == 1
    assert budget.stats.requests == 1
    assert budget.stats.rejected == 1


def test_sync_budget_allows_when_available(fake_clock: FakeClock) -> None:
    budget = RetryBudget(ratio=0, min_per_second=1, window=10)
    inner = Inner(Response(503), Response(200))
    RetryTransport(inner, HttpRetryPolicy(budget=budget)).handle_request(Request())
    assert inner.calls == 2


def test_sync_deadline_respected(fake_clock: FakeClock) -> None:
    resp = Response(503)
    inner = Inner(Response(503), resp)
    transport = RetryTransport(inner, HttpRetryPolicy(delay=1, backoff=10))
    with deadline(5):
        # first wait 1s fits; second wait 10s would outlast the 4s left
        assert transport.handle_request(Request()) is resp
    assert fake_clock.sleeps == [1]


def test_sync_events(fake_clock: FakeClock) -> None:
    seen: list[Any] = []
    inner = Inner(Response(503), Response(200))
    with listening(seen.append):
        RetryTransport(inner, HttpRetryPolicy(delay=1)).handle_request(Request())
    names = [e.name for e in seen]
    assert names == ["retry.attempt_failed", "retry.succeeded"]
    assert seen[0].function == "http GET api.example.com"
    assert seen[0].fields["status"] == 503 and seen[0].fields["next_delay"] == 1

    seen.clear()
    with listening(seen.append):
        RetryTransport(Inner(Response(503)), HttpRetryPolicy(max_attempts=1)).handle_request(
            Request()
        )
    assert [e.name for e in seen] == ["retry.gave_up"]


def test_sync_close_and_context() -> None:
    inner = Inner()
    with RetryTransport(inner):
        pass
    assert inner.closed


# ---------------------------------------------------------------- async transport


async def test_async_retries_and_closes(fake_clock: FakeClock) -> None:
    bad, good = Response(503), Response(200)
    transport = AsyncRetryTransport(Inner(bad, good), HttpRetryPolicy(delay=2))
    assert await transport.handle_async_request(Request()) is good
    assert fake_clock.sleeps == [2]
    assert bad.closed


async def test_async_exception_and_post(fake_clock: FakeClock) -> None:
    inner = Inner(ConnectionError("a"), Response(200))
    out = await AsyncRetryTransport(inner).handle_async_request(Request())
    assert out.status_code == 200
    inner = Inner(ConnectionError("a"))
    with pytest.raises(ConnectionError):
        await AsyncRetryTransport(inner).handle_async_request(Request("POST"))
    assert inner.calls == 1


async def test_async_budget_and_deadline(fake_clock: FakeClock) -> None:
    budget = RetryBudget(ratio=0, min_per_second=0)
    inner = Inner(Response(503))
    transport = AsyncRetryTransport(inner, HttpRetryPolicy(budget=budget))
    assert (await transport.handle_async_request(Request())).status_code == 503
    assert inner.calls == 1

    inner = Inner(Response(503))
    transport = AsyncRetryTransport(inner, HttpRetryPolicy(delay=10))
    with deadline(5):
        assert (await transport.handle_async_request(Request())).status_code == 503
    assert inner.calls == 1


async def test_async_aclose_context() -> None:
    inner = Inner()
    async with AsyncRetryTransport(inner):
        pass
    assert inner.closed


# ---------------------------------------------------------------- requests adapter


def _install_fake_requests(monkeypatch: pytest.MonkeyPatch) -> type:
    class HTTPAdapter:
        script: ClassVar[list[Any]] = []
        calls = 0

        def send(self, request: Any, **kwargs: Any) -> Any:
            type(self).calls += 1
            item = type(self).script.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

    adapters = types.ModuleType("requests.adapters")
    adapters.HTTPAdapter = HTTPAdapter  # type: ignore[attr-defined]
    package = types.ModuleType("requests")
    package.adapters = adapters  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "requests", package)
    monkeypatch.setitem(sys.modules, "requests.adapters", adapters)
    return HTTPAdapter


class FakeSession:
    def __init__(self) -> None:
        self.mounts: dict[str, Any] = {}

    def mount(self, prefix: str, adapter: Any) -> None:
        self.mounts[prefix] = adapter


class RequestsRequest:
    def __init__(self, method: str = "GET", headers: dict[str, str] | None = None) -> None:
        self.method = method
        self.headers = headers or {}
        self.url = "https://api.example.com/items"


def test_mount_retries_retries_and_mounts(
    monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    base = _install_fake_requests(monkeypatch)
    bad, good = Response(503), Response(200)
    base.script = [bad, good]
    session = FakeSession()
    adapter = mount_retries(session, HttpRetryPolicy(delay=3))
    assert set(session.mounts) == {"http://", "https://"}
    assert session.mounts["https://"] is adapter
    assert isinstance(adapter, base)
    assert adapter.send(RequestsRequest()) is good
    assert fake_clock.sleeps == [3] and bad.closed


def test_mount_retries_post_and_exceptions(
    monkeypatch: pytest.MonkeyPatch, fake_clock: FakeClock
) -> None:
    base = _install_fake_requests(monkeypatch)
    adapter = mount_retries(FakeSession(), prefixes=("https://api.example.com",))
    base.script = [Response(503)]
    assert adapter.send(RequestsRequest("POST")).status_code == 503
    base.script = [ConnectionError("x"), Response(200)]
    assert adapter.send(RequestsRequest()).status_code == 200
    base.script = [ConnectionError("x")] * 3
    with pytest.raises(ConnectionError):
        adapter.send(RequestsRequest())


def test_mount_retries_without_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "requests", None)
    monkeypatch.setitem(sys.modules, "requests.adapters", None)
    with pytest.raises(ImportError):
        mount_retries(FakeSession())


# ---------------------------------------------------------------- http_retry


class AioResponse:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}


async def test_http_retry_async_status_and_retry_after(fake_clock: FakeClock) -> None:
    script = [AioResponse(429, {"Retry-After": "4"}), AioResponse(503), AioResponse(200)]

    @http_retry(HttpRetryPolicy(delay=1))
    async def fetch() -> AioResponse:
        return script.pop(0)

    assert (await fetch()).status == 200
    assert fake_clock.sleeps == [4, 2]


async def test_http_retry_exceptions_by_name(fake_clock: FakeClock) -> None:
    calls = 0

    @http_retry(HttpRetryPolicy(delay=1))
    async def fetch() -> AioResponse:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectError("down")
        return AioResponse(200)

    assert (await fetch()).status == 200
    assert fake_clock.sleeps == [1, 2]


def test_http_retry_exhausted_returns_last(fake_clock: FakeClock) -> None:
    @http_retry(HttpRetryPolicy(max_attempts=2, delay=1))
    def fetch() -> AioResponse:
        return AioResponse(503)

    assert fetch().status == 503
    assert fake_clock.sleeps == [1]


def test_http_retry_retry_after_too_long_not_retried(fake_clock: FakeClock) -> None:
    calls = 0

    @http_retry()
    def fetch() -> AioResponse:
        nonlocal calls
        calls += 1
        return AioResponse(429, {"Retry-After": "3600"})

    assert fetch().status == 429
    assert calls == 1


def test_http_retry_refuses_non_idempotent(fake_clock: FakeClock) -> None:
    calls = 0

    @http_retry(method="POST")
    def create() -> AioResponse:
        nonlocal calls
        calls += 1
        return AioResponse(503)

    assert create().status == 503 and calls == 1

    @http_retry(method="POST", idempotent=True)
    def create_keyed() -> AioResponse:
        nonlocal calls
        calls += 1
        return AioResponse(503 if calls < 3 else 200)

    calls = 0
    assert create_keyed().status == 200 and calls == 3

    @http_retry(method="GET", idempotent=False)
    def forbidden() -> AioResponse:
        return AioResponse(503)

    assert forbidden().status == 503


def test_http_retry_budget(fake_clock: FakeClock) -> None:
    budget = RetryBudget(ratio=0, min_per_second=0)
    calls = 0

    @http_retry(HttpRetryPolicy(budget=budget))
    def fetch() -> AioResponse:
        nonlocal calls
        calls += 1
        return AioResponse(503)

    assert fetch().status == 503 and calls == 1


# ---------------------------------------------------------------- real httpx (optional)


def test_real_httpx_transport(fake_clock: FakeClock) -> None:
    httpx = pytest.importorskip("httpx")
    attempts = 0

    def handler(request: Any) -> Any:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts < 3 else 200, json={"ok": True})

    client = httpx.Client(
        transport=RetryTransport(httpx.MockTransport(handler), HttpRetryPolicy(delay=1))
    )
    assert client.get("https://api.example.com/x").status_code == 200
    assert attempts == 3 and fake_clock.sleeps == [1, 2]
    attempts = 0
    assert client.post("https://api.example.com/x").status_code == 503
    assert attempts == 1


async def test_real_httpx_async_transport(fake_clock: FakeClock) -> None:
    httpx = pytest.importorskip("httpx")
    attempts = 0

    def handler(request: Any) -> Any:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts < 2 else 200)

    transport = AsyncRetryTransport(httpx.MockTransport(handler), HttpRetryPolicy(delay=1))
    async with httpx.AsyncClient(transport=transport) as client:
        assert (await client.get("https://api.example.com/x")).status_code == 200
    assert attempts == 2
