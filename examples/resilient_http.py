"""A resilient HTTP client built from python-wrap decorators, runnable offline.

Retries transient failures with exponential backoff and jitter, keeps one circuit
breaker per host, rate limits per host and puts an overall deadline on each call. The
network is a scripted fake transport, so nothing leaves your machine. To use it for
real, replace ``FakeTransport`` with a thin adapter around httpx or requests (see
docs/cookbook/resilient-http-client.md).

Run: python examples/resilient_http.py
"""

from __future__ import annotations

import threading
from urllib.parse import urlsplit

from python_wrap import CircuitBreaker, CircuitOpenError, rate_limit, retry, timeout
from python_wrap.testing import fake_clock


class Response:
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self.body = body


class ServerError(Exception):
    """A 5xx answer: worth retrying."""


class ClientError(Exception):
    """A 4xx answer: our fault, retrying cannot help."""


TRANSIENT = (ConnectionError, TimeoutError, ServerError)


def host_of(url: str) -> str:
    return urlsplit(url).netloc


class FakeTransport:
    """Plays back a script per host: an int is a status code, an exception is raised."""

    def __init__(self, script: dict[str, list[object]]) -> None:
        self.script = script
        self.calls: list[str] = []

    def send(self, url: str, *, timeout: float) -> Response:
        self.calls.append(url)
        steps = self.script[host_of(url)]
        step = steps.pop(0) if len(steps) > 1 else steps[0]  # the last step repeats
        if isinstance(step, BaseException):
            raise step
        assert isinstance(step, int)
        return Response(step, f"body of {url}")


class ResilientClient:
    def __init__(self, transport: FakeTransport) -> None:
        self._transport = transport
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def breaker_for(self, host: str) -> CircuitBreaker:
        with self._lock:
            if host not in self._breakers:
                self._breakers[host] = CircuitBreaker(
                    failure_threshold=3, recovery_timeout=30, on=TRANSIENT, name=host
                )
            return self._breakers[host]

    @timeout(30)  # overall deadline, retries included
    @retry(max_attempts=4, delay=0.2, backoff=2, max_delay=5, jitter=0.1, on=TRANSIENT)
    @rate_limit(5, 1.0, block=True, key=lambda self, url: host_of(url))
    def get(self, url: str) -> Response:
        # CircuitOpenError is not in TRANSIENT, so an open circuit is not retried.
        return self.breaker_for(host_of(url)).call(self._send, url)

    def _send(self, url: str) -> Response:
        response = self._transport.send(url, timeout=5.0)
        if response.status >= 500:
            raise ServerError(f"{response.status} from {url}")
        if response.status >= 400:
            raise ClientError(f"{response.status} from {url}")
        return response


# --- demo ---


def main() -> None:
    transport = FakeTransport(
        {
            "api.example.com": [503, ConnectionError("reset"), 200],
            "down.example.com": [503],
            "missing.example.com": [404],
        }
    )
    client = ResilientClient(transport)

    with fake_clock() as clock:  # backoff sleeps are instant and recorded
        # 1. Transient failures are retried with growing, jittered waits.
        assert client.get("https://api.example.com/users").status == 200
        print("retried twice, waits:", [round(s, 2) for s in clock.sleeps])

        # 2. A 404 is not transient: it is raised at once, not retried.
        before = len(transport.calls)
        try:
            client.get("https://missing.example.com/x")
        except ClientError:
            assert len(transport.calls) == before + 1
        print("404 raised without retrying")

        # 3. A dead host opens its breaker; other hosts are unaffected.
        try:
            client.get("https://down.example.com/a")
        except CircuitOpenError as exc:
            print(f"circuit for down.example.com open, retry in {exc.retry_after:.0f}s")
        before = len(transport.calls)
        try:
            client.get("https://down.example.com/b")
        except CircuitOpenError:
            assert len(transport.calls) == before  # rejected without touching the network
        assert client.get("https://api.example.com/users").status == 200

        # 4. After the recovery timeout one trial call is allowed through.
        transport.script["down.example.com"] = [200]
        clock.advance(31)
        assert client.get("https://down.example.com/c").status == 200
        print("circuit closed again after recovery")

        # 5. The rate limit waits (it does not fail) once a host's budget is used up.
        clock.sleeps.clear()
        for i in range(7):
            client.get(f"https://api.example.com/page/{i}")
        assert any(s > 0 for s in clock.sleeps)
        print("rate limit waited:", [round(s, 2) for s in clock.sleeps])


if __name__ == "__main__":
    main()
