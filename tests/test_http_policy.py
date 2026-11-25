"""HttpPolicy: breaker, limiter, bulkhead, timeouts and propagation around retries."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from python_wrap.circuit import CircuitBreaker
from python_wrap.deadline import deadline
from python_wrap.errors import CircuitOpenError
from python_wrap.http import (
    AsyncPolicyTransport,
    HttpPolicy,
    HttpRetryPolicy,
    PolicyTransport,
    RetryTransport,
)
from python_wrap.logs import log_context


class Req:
    def __init__(self, host: str, method: str = "GET") -> None:
        self.url = type("U", (), {"host": host})()
        self.method, self.headers, self.extensions = method, {}, {}


class Resp:
    def __init__(self, status: int) -> None:
        self.status_code, self.headers = status, {}


class Inner:
    def __init__(self) -> None:
        self.hosts: list[str] = []

    def handle_request(self, request: Any) -> Resp:
        self.hosts.append(request.url.host)
        return Resp(503 if request.url.host == "bad" else 200)

    async def handle_async_request(self, request: Any) -> Resp:
        return self.handle_request(request)


def _policy(**kw: Any) -> HttpPolicy:
    return HttpPolicy(
        retry=HttpRetryPolicy(max_attempts=1),
        breaker=lambda name: CircuitBreaker(failure_threshold=5, name=name),
        per_host=True,
        **kw,
    )


def test_per_host_breaker_opens_for_one_host_only() -> None:
    inner = Inner()
    transport = PolicyTransport(inner, _policy())
    for _ in range(5):
        assert transport.handle_request(Req("bad")).status_code == 503
    with pytest.raises(CircuitOpenError):
        transport.handle_request(Req("bad"))
    assert transport.handle_request(Req("good")).status_code == 200
    assert inner.hosts.count("bad") == 5


def test_4xx_never_counts_as_failure() -> None:
    inner = Inner()
    inner.handle_request = lambda request: Resp(404)  # type: ignore[method-assign]
    transport = PolicyTransport(inner, _policy())
    for _ in range(10):
        assert transport.handle_request(Req("x")).status_code == 404


def test_timeouts_default_and_shorten_to_deadline() -> None:
    transport = PolicyTransport(Inner(), HttpPolicy(read_timeout=7, connect_timeout=3))
    request = Req("good")
    transport.handle_request(request)
    assert request.extensions["timeout"] == {"read": 7, "connect": 3}
    with deadline(2):
        request = Req("good")
        transport.handle_request(request)
    assert request.extensions["timeout"]["read"] <= 2


def test_propagation_headers() -> None:
    transport = PolicyTransport(Inner(), HttpPolicy(propagate=True))
    request = Req("good")
    with log_context(request_id="r-1"), deadline(1.5):
        transport.handle_request(request)
    assert request.headers["X-Request-ID"] == "r-1"
    assert 0 < int(request.headers["X-Request-Deadline"]) <= 1500


def test_retry_transport_is_a_thin_wrapper() -> None:
    retry = HttpRetryPolicy(max_attempts=2, delay=0)
    transport = RetryTransport(Inner(), retry)
    assert transport.policy is retry
    assert transport.handle_request(Req("good")).status_code == 200


def test_async_transport() -> None:
    transport = AsyncPolicyTransport(Inner(), _policy())

    async def main() -> int:
        return (await transport.handle_async_request(Req("good"))).status_code

    assert asyncio.run(main()) == 200


def test_per_host_rejects_a_single_shared_object() -> None:
    with pytest.raises(ValueError):
        HttpPolicy(breaker=CircuitBreaker(name="x"), per_host=True)
