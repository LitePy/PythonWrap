"""Story: a resilient HTTP client survives a flaky upstream, a dead host and a burst."""

from __future__ import annotations

import threading
from urllib.parse import urlsplit

import pytest

from python_wrap import CircuitBreaker, CircuitOpenError, rate_limit, retry, timeout
from python_wrap.testing import FakeClock

pytestmark = pytest.mark.e2e


class ServerError(Exception):
    pass


class ClientError(Exception):
    pass


TRANSIENT = (ConnectionError, TimeoutError, ServerError)


def host_of(url: str) -> str:
    return urlsplit(url).netloc


class FakeTransport:
    """Scripted per host: an int is a status code, an exception is raised; last step repeats."""

    def __init__(self, script: dict[str, list[object]]) -> None:
        self.script = script
        self.calls: list[str] = []

    def send(self, url: str) -> int:
        self.calls.append(url)
        steps = self.script[host_of(url)]
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        if isinstance(step, BaseException):
            raise step
        assert isinstance(step, int)
        return step


class Client:
    def __init__(self, transport: FakeTransport) -> None:
        self.transport = transport
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def breaker_for(self, host: str) -> CircuitBreaker:
        with self._lock:
            return self._breakers.setdefault(
                host,
                CircuitBreaker(failure_threshold=3, recovery_timeout=30, on=TRANSIENT, name=host),
            )

    @timeout(30)
    @retry(max_attempts=4, delay=0.2, backoff=2, max_delay=5, on=TRANSIENT)
    @rate_limit(5, 1.0, block=True, key=lambda self, url: host_of(url))
    def get(self, url: str) -> int:
        return self.breaker_for(host_of(url)).call(self._send, url)

    def _send(self, url: str) -> int:
        status = self.transport.send(url)
        if status >= 500:
            raise ServerError(str(status))
        if status >= 400:
            raise ClientError(str(status))
        return status


@pytest.fixture
def client() -> Client:
    return Client(
        FakeTransport(
            {
                "api.test": [503, ConnectionError("reset"), 200],
                "down.test": [503],
                "gone.test": [404],
            }
        )
    )


def test_transient_failures_are_retried_with_growing_waits(
    client: Client, fake_clock: FakeClock
) -> None:
    assert client.get("https://api.test/users") == 200
    assert len(client.transport.calls) == 3
    assert fake_clock.sleeps == [0.2, 0.4]


def test_client_errors_are_not_retried(client: Client, fake_clock: FakeClock) -> None:
    with pytest.raises(ClientError):
        client.get("https://gone.test/x")
    assert len(client.transport.calls) == 1
    assert fake_clock.sleeps == []


def test_dead_host_opens_its_breaker_and_recovers(client: Client, fake_clock: FakeClock) -> None:
    with pytest.raises(CircuitOpenError):
        client.get("https://down.test/a")
    calls = len(client.transport.calls)
    with pytest.raises(CircuitOpenError):
        client.get("https://down.test/b")
    assert len(client.transport.calls) == calls  # rejected without touching the network
    assert client.get("https://api.test/users") == 200  # other hosts unaffected

    client.transport.script["down.test"] = [200]
    fake_clock.advance(31)
    assert client.get("https://down.test/c") == 200


def test_rate_limit_waits_instead_of_failing(client: Client, fake_clock: FakeClock) -> None:
    client.transport.script["api.test"] = [200]
    for i in range(7):
        assert client.get(f"https://api.test/page/{i}") == 200
    assert any(s > 0 for s in fake_clock.sleeps)
