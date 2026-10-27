# Testing code that uses these decorators

Decorators that wait or remember make tests slow or order-dependent. python-wrap is built so you can fix both without touching production code.

## Do not sleep: patch the clock

Every time-based decorator reads time and waits through `python_wrap._core`, always as module attributes:

| Attribute | Used for |
| --- | --- |
| `_core.monotonic()` | Rate-limit windows, throttle, cache TTL, circuit recovery, retry `elapsed` |
| `_core.perf_counter()` | `timing`, `benchmark`, `audit` durations |
| `_core.sleep(seconds)` | Waiting in sync `retry` and `rate_limit(block=True)` |
| `_core.async_sleep(seconds)` | The same, for `async def` functions |

Patch those four and tests run instantly and deterministically. `_core` is internal plumbing, but these four names are the testing hook its module docstring documents.

The actual deadlines of `timeout` (signal timer, worker thread, `asyncio.wait_for`) and `debounce` (`threading.Timer`, event-loop tasks) use real timers and are not affected by the patch. Test them with tiny real durations, or call `debounce`'s `.flush()`.

```python
from unittest import mock
from python_wrap import _core, retry

sleeps: list[float] = []
calls = []

@retry(max_attempts=4, delay=1.0, backoff=2, on=ConnectionError)
def flaky() -> str:
    calls.append(1)
    if len(calls) < 4:
        raise ConnectionError
    return "ok"

with mock.patch.object(_core, "sleep", sleeps.append):
    assert flaky() == "ok"            # returns at once; no real waiting

assert sleeps == [1.0, 2.0, 4.0]      # the backoff schedule you configured
```

### A reusable fake clock

A fixture that advances time instead of waiting (this is the one python-wrap's own tests use):

<!-- skip -->
```python
# conftest.py
from dataclasses import dataclass, field

import pytest
from python_wrap import _core


@dataclass
class FakeClock:
    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(seconds, 0.0)

    async def async_sleep(self, seconds: float) -> None:
        self.sleep(seconds)


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(_core, "monotonic", clock.time)
    monkeypatch.setattr(_core, "perf_counter", clock.time)
    monkeypatch.setattr(_core, "sleep", clock.sleep)
    monkeypatch.setattr(_core, "async_sleep", clock.async_sleep)
    return clock
```

With it, you can test expiry and recovery without waiting:

```python
from unittest import mock
from python_wrap import _core, memoize

now = [1000.0]
calls = []

@memoize(ttl=60)
def load() -> int:
    calls.append(1)
    return len(calls)

with mock.patch.object(_core, "monotonic", lambda: now[0]):
    assert load() == 1
    now[0] += 59
    assert load() == 1          # still fresh
    now[0] += 2
    assert load() == 2          # expired: recomputed
```

The same trick moves a circuit breaker from open to half-open:

```python
from unittest import mock
from python_wrap import CircuitBreaker, CircuitOpenError, CircuitState, _core

now = [0.0]
breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30)

@breaker
def call() -> None:
    raise ConnectionError

with mock.patch.object(_core, "monotonic", lambda: now[0]):
    try:
        call()
    except ConnectionError:
        pass
    assert breaker.state is CircuitState.OPEN
    try:
        call()
    except CircuitOpenError as exc:
        assert exc.retry_after == 30

    now[0] += 31
    assert breaker.state is CircuitState.HALF_OPEN
```

!!! tip "Async"
    Patch `_core.async_sleep` with an `async def` that records and returns (as `FakeClock.async_sleep` does) so `async def` retries do not wait either.

## Reset state between tests

State lives on the decorated function, so it outlives a single test. Reset it in a fixture.

| State | Reset with |
| --- | --- |
| `memoize` / `cache` entries and statistics | `func.cache_clear()` (or `func.cache_delete(*args)` for one entry) |
| `once` result | `func.reset()` |
| Circuit breaker | `breaker.reset()` (or `func.breaker.reset()` for `@circuit_breaker`) |
| `debounce` pending call | `func.cancel()` |

```python
from python_wrap import CircuitState, circuit_breaker, memoize, once

@memoize
def lookup(n: int) -> int:
    return n

@once
def setup() -> object:
    return object()

@circuit_breaker(failure_threshold=1)
def call() -> None:
    raise RuntimeError

lookup(1); setup()
try:
    call()
except RuntimeError:
    pass
assert call.breaker.state is CircuitState.OPEN

# What an autouse fixture would do after every test:
lookup.cache_clear()
setup.reset()
call.breaker.reset()

assert lookup.cache_info().currsize == 0
assert setup.called is False
assert call.breaker.state is CircuitState.CLOSED
```

<!-- skip -->
```python
# conftest.py
import pytest

from myapp.api import call_api, lookup, setup_once


@pytest.fixture(autouse=True)
def reset_python_wrap_state():
    yield
    lookup.cache_clear()
    setup_once.reset()
    call_api.breaker.reset()
```

`rate_limit`, `throttle` and `singleflight` have no reset method. Use the fake clock to move past the window, or build a fresh decorated function inside the test.

## Disabling `mock_data`

`mock_data(enabled=...)` is evaluated on **every call**, so a test only has to flip whatever it reads:

```python
import os
from python_wrap import mock_data

@mock_data({"id": 1}, enabled=lambda: os.environ.get("USE_MOCKS") == "1")
def fetch() -> dict:
    return {"id": 2}

os.environ["USE_MOCKS"] = "1"
assert fetch() == {"id": 1}

os.environ["USE_MOCKS"] = "0"      # tests of the real behaviour
assert fetch() == {"id": 2}
```

In pytest, use `monkeypatch.setenv("USE_MOCKS", "0")` so the change is undone afterwards.

## Bypass a decorator

Wrapped functions keep `__wrapped__` (they use `functools.wraps`), so you can call the undecorated function in a unit test:

```python
from python_wrap import retry

calls = []

@retry(max_attempts=3, delay=0)
def flaky() -> None:
    calls.append(1)
    raise ValueError

try:
    flaky.__wrapped__()            # skips the retry loop
except ValueError:
    pass
assert len(calls) == 1
```

## Capturing logs

python-wrap logs to the `python_wrap` logger (or your `logger=`), so pytest's `caplog` works as usual:

<!-- skip -->
```python
import logging

def test_retry_logs_a_warning(caplog):
    caplog.set_level(logging.WARNING, logger="python_wrap")
    ...
    assert "retrying in" in caplog.text
```

## Silence deprecation warnings you expect

<!-- skip -->
```python
import pytest

def test_old_api_warns():
    with pytest.warns(DeprecationWarning, match="old_func"):
        old_func()
```

`deprecate` points the warning at the caller's line, so `-W error::DeprecationWarning` shows which test line used the old API.
