# A resilient HTTP client

## The problem

Your service calls other services. They return 503 during deploys, drop connections, answer slowly, and sometimes stay down for minutes. A bare `requests.get` turns every one of those into an error for your user, and a naive retry loop makes an outage worse by hammering the dependency.

You want a client that:

- retries **transient** failures (connection resets, timeouts, 5xx) but never 4xx,
- waits longer between attempts and adds jitter so a fleet of clients does not retry in lockstep,
- stops calling a host that is clearly down, **per host**, so one bad dependency does not block the others,
- never exceeds a request budget per host,
- has an overall deadline.

## The code

The network is a scripted `FakeTransport`, so this page runs offline and the behaviour is deterministic. The client only needs an object with `send(url, *, timeout) -> Response`; [mapping it to httpx or requests](#using-httpx-or-requests) is a ten-line adapter.

```python
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


main()
```

## How it works

The decorators on `get` read from the bottom up, because the innermost one runs first on each attempt:

1. **`@rate_limit(5, 1.0, block=True, key=...)`** allows 5 calls per second per host. `key=` receives the call's arguments (including `self`) and returns the bucket; here it is the host. With `block=True` the call waits (it does not fail), and in async code it waits with `await`, so the event loop stays free.
2. **`@retry(..., on=TRANSIENT)`** wraps the rate limiter, so every attempt, including retries, counts against the host's budget. The wait before attempt `n + 1` is `min(delay * backoff ** (n - 1), max_delay) + uniform(0, jitter)`: roughly 0.2 s, 0.4 s, 0.8 s, each plus up to 0.1 s of jitter. `on=` limits retries to the listed exception types; a `ClientError` (4xx) propagates immediately.
3. **`@timeout(30)`** is outermost, so it bounds the whole call, retries and waits included.

The circuit breaker is not a decorator on `get` because there must be **one breaker per host**, created on demand. `breaker.call(func, *args)` runs a call through a breaker without decorating anything. Because `on=TRANSIENT` is set on the breaker too, a 404 does not count as a failure: only problems that suggest the host is unhealthy trip it.

Notice what happens when a host is down. The first three failed attempts open its breaker (`failure_threshold=3`). The fourth attempt raises `CircuitOpenError`, which is **not** in `TRANSIENT`, so `retry` lets it through instead of waiting out more backoff, and later calls fail instantly with `exc.retry_after` telling the caller how long until the circuit tries again.

## Pitfalls

!!! warning "Rate-limit state belongs to the function, not the instance"
    `@rate_limit` on a method keeps its buckets on the decorated function, so every `ResilientClient` instance shares them (separated only by your `key=`). That is usually what you want for "5 calls per second to this host from this process", but it surprises tests. Use `disable_rate_limits()` or a unique host name per test. For a limit per instance, include `id(self)` in the key.

!!! warning "`timeout` on sync code uses signals"
    On Unix, in the main thread, `@timeout` interrupts the call with `SIGALRM`. In worker threads, or on Windows, it falls back to a helper thread and **cannot stop the work**, only stop waiting for it. Set a real timeout on the HTTP library as well (the `timeout=5.0` passed to the transport above); the decorator is the overall budget, not a replacement. In `async def` code `@timeout` cancels the coroutine properly.

!!! warning "Only retry what is safe to repeat"
    Retrying a `GET` is safe. Retrying a `POST` that charges a card is not, unless the endpoint is idempotent (for example it accepts an idempotency key). Give write methods a narrower `on=` (for example only `ConnectionError` raised before the request was sent) or no retry at all.

!!! note "Honouring `Retry-After`"
    `retry` computes its waits from `delay`, `backoff` and `jitter`; it cannot take a delay from the exception. If a server sends `Retry-After` and you must obey it, catch that case yourself and sleep, or raise a non-retried exception for it.

* Keep `failure_threshold` and `recovery_timeout` honest. Three failures out of three calls is a dead host; three failures out of 3000 is noise. The breaker counts **consecutive** failures, so a single success resets it.
* Do not create a new `CircuitBreaker` on every request: it would never see enough failures. Keep them in a dict (as above) or module-level.

## Using httpx or requests

Everything above the transport stays the same. Write an adapter that turns the library's exceptions into the built-in ones the client retries on.

<!-- skip -->
```python
import httpx

class HttpxTransport:
    def __init__(self) -> None:
        self._client = httpx.Client()

    def send(self, url: str, *, timeout: float) -> Response:
        try:
            r = self._client.get(url, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise TimeoutError(str(exc)) from exc
        except httpx.TransportError as exc:  # connect errors, resets, DNS
            raise ConnectionError(str(exc)) from exc
        return Response(r.status_code, r.text)

client = ResilientClient(HttpxTransport())
```

<!-- skip -->
```python
import requests

class RequestsTransport:
    def __init__(self) -> None:
        self._session = requests.Session()

    def send(self, url: str, *, timeout: float) -> Response:
        try:
            r = self._session.get(url, timeout=timeout)
        except requests.Timeout as exc:
            raise TimeoutError(str(exc)) from exc
        except requests.ConnectionError as exc:
            raise ConnectionError(str(exc)) from exc
        return Response(r.status_code, r.text)
```

For `httpx.AsyncClient` make `get` and `_send` `async def` and use `await breaker.call(...)`: every decorator used here works unchanged on coroutine functions.

## Test it

The decorators wait and remember, so tests need the helpers from [`python_wrap.testing`](../testing.md): `fake_clock` makes sleeps instant and records them, `spy_on` counts transport calls and `disable_rate_limits` lets calls through limiters. (`fast_mode` skips waits without recording them; see [Testing code that uses python-wrap](testing-your-code.md).)

```python
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

import pytest

from python_wrap.testing import disable_rate_limits, spy_on


def make(script: dict[str, list[object]]) -> tuple[ResilientClient, FakeTransport]:
    transport = FakeTransport(script)
    return ResilientClient(transport), transport


def test_retries_with_exponential_backoff() -> None:
    client, transport = make({"a.test": [503, ConnectionError("reset"), 200]})
    with fake_clock() as clock, disable_rate_limits():
        assert client.get("https://a.test/x").status == 200
    assert len(transport.calls) == 3
    first, second = clock.sleeps
    assert 0.2 <= first <= 0.3  # delay 0.2 + up to 0.1 jitter
    assert 0.4 <= second <= 0.5  # backoff doubles it


def test_client_errors_are_not_retried() -> None:
    client, transport = make({"b.test": [404]})
    with spy_on(transport, "send") as send, fake_clock() as clock, disable_rate_limits():
        with pytest.raises(ClientError):
            client.get("https://b.test/missing")
    assert send.call_count == 1
    assert clock.sleeps == []


def test_open_circuit_fails_fast_and_is_per_host() -> None:
    client, transport = make({"down.test": [503], "up.test": [200]})
    with fake_clock() as clock, disable_rate_limits():
        with pytest.raises(CircuitOpenError):
            client.get("https://down.test/x")
        calls_when_open = len(transport.calls)
        with pytest.raises(CircuitOpenError):
            client.get("https://down.test/y")
        assert len(transport.calls) == calls_when_open  # no network traffic
        assert client.get("https://up.test/").status == 200  # other hosts unaffected

        transport.script["down.test"] = [200]
        clock.advance(31)  # past recovery_timeout
        assert client.get("https://down.test/z").status == 200


def test_rate_limit_waits_instead_of_failing() -> None:
    client, _ = make({"rate.test": [200]})
    with fake_clock() as clock:  # rate limiting stays on here
        for i in range(6):
            client.get(f"https://rate.test/{i}")
    assert any(s > 0.5 for s in clock.sleeps)


# pytest collects the test_ functions itself; this loop only lets the page run them directly.
for _name, _test in list(globals().items()):
    if _name.startswith("test_"):
        _test()
```

If you use pytest, run it as usual; in a larger suite enable the plugin once (`pytest_plugins = ["python_wrap.testing"]` in `conftest.py`) to get the `pw_fake_clock` and `pw_fast_mode` fixtures instead of the context managers.
