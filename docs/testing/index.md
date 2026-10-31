# Testing code that uses these decorators

Decorators that wait or remember make tests slow or order-dependent. python-wrap is built so you can fix both without touching production code.

python-wrap ships its own testing helpers in `python_wrap.testing` (standard library only; pytest is optional). They cover the common jobs: skip the waiting, cap retries, switch off rate limits and caches, control time, observe calls and wait for background work.

For a worked example of testing a whole service built on these decorators, see the cookbook recipe [Testing your code](../cookbook/testing-your-code.md).

## Fast mode: instant waits, retries still run

`fast_mode()` makes every wait inside python-wrap instant (`no_sleep`). Retries still run the configured number of times and limiters still count; nothing actually sleeps. Use it as a context manager or a decorator on sync and async tests:

```python
import time
from python_wrap import retry
from python_wrap.testing import fast_mode

calls = []

@retry(max_attempts=4, delay=30, backoff=2, on=ConnectionError)
def flaky() -> str:
    calls.append(1)
    if len(calls) < 4:
        raise ConnectionError
    return "ok"

start = time.monotonic()
with fast_mode():
    assert flaky() == "ok"          # would take 30 + 60 + 120 seconds
assert time.monotonic() - start < 5
```

The other switches work the same way:

| Helper | Effect |
| --- | --- |
| `fast_mode()` | `_core.sleep` / `_core.async_sleep` return immediately |
| `disable_retries()` | `retry` makes a single attempt |
| `disable_rate_limits()` | `rate_limit` / `throttle` let every call through |
| `disable_caching()` | `memoize` / `cache` always call the wrapped function |
| `overrides(no_sleep=..., max_attempts=..., ...)` | Any combination, in one scope |

```python
import asyncio
from python_wrap import _core
from python_wrap.testing import disable_retries, fast_mode, overrides

with overrides(no_sleep=True, max_attempts=3):
    with disable_retries():
        assert _core.overrides.max_attempts == 1
    assert _core.overrides.max_attempts == 3      # restored on exit
assert _core.overrides.max_attempts is None

@fast_mode()
async def test_something() -> None:
    assert _core.overrides.no_sleep

asyncio.run(test_something())
```

!!! note "Nesting and threads"
    The overrides are one process-wide state, not context variables, so worker threads started by your test see them. Each scope restores exactly the fields it changed, to the values they had on entry, so scopes nest. They must exit in reverse order of entry (`with` blocks and decorators always do). Overlapping scopes from different threads that touch the same field are not isolated from each other; enter them from the test, not from concurrent workers.

## Fake clock

`fake_clock()` replaces `_core.monotonic`, `_core.perf_counter`, `_core.wall_time`, `_core.sleep` and `_core.async_sleep`. Sleeping advances the clock and records the duration, so you can assert the exact backoff schedule. The async sleep yields to the event loop, so other tasks still run.

```python
from python_wrap import retry
from python_wrap.testing import fake_clock

calls = []

@retry(max_attempts=4, delay=1.0, backoff=2, on=ConnectionError)
def flaky() -> str:
    calls.append(1)
    if len(calls) < 4:
        raise ConnectionError
    return "ok"

with fake_clock() as clock:
    start = clock.now
    assert flaky() == "ok"            # returns at once; no real waiting

assert clock.sleeps == [1.0, 2.0, 4.0]      # the backoff schedule you configured
assert clock.now == start + 7.0
```

`FakeClock()` starts at the real `time.monotonic()` of the moment it is created, rounded up to a whole second (so differences stay exact), so entries stamped before it was installed are in its past and still expire; pass `start=` for a fixed reading. It has `.now`, `.advance(seconds)`, `.sleeps` and `.total_slept`; pass your own instance with `fake_clock(clock)`.

## What the overrides affect

python-wrap separates two kinds of waiting:

- **Backoff waits** go through `_core.sleep` / `_core.async_sleep`: `retry` delays and a blocking rate limiter (`block=True`) waiting for a slot. `fast_mode()` makes them instant; `fake_clock()` turns them into clock advances you can assert on.
- **Limits on how long to wait** use real timers: the deadline of `timeout` (signal timer, worker thread, `asyncio.timeout`), `Bulkhead(timeout=...)` / `limit_concurrency` waiting for a slot, `race(timeout=...)`, the `hedge` delay before a backup attempt, the `batch_calls` window and the `debounce` quiet period. Neither `fast_mode()` nor `fake_clock()` changes them, and sync and async code behave the same. Making them instant would turn "wait at most 5 s" into "fail at once" (or "hedge at once"). Test them with tiny real durations, or call `.flush()` / `.aflush()` on `debounce` and `batch_calls`.

| Behaviour | `fast_mode()` | `fake_clock()` |
| --- | --- | --- |
| `retry` backoff | instant | advances the clock |
| `rate_limit(block=True)` / `RateLimiter(block=True)` waiting | the call goes through at once and is counted | advances the clock, then goes through |
| `rate_limit` / `throttle` non-blocking rejection | still rejects | windows follow the fake clock |
| TTLs, circuit breaker recovery, `deadline` remaining time | real time | follow the fake clock |
| `timeout`, `Bulkhead` / `race` timeouts, `hedge` delay, `batch_calls` / `debounce` windows | real time | real time |

Bulkhead and other concurrency limits are not rate limits: `disable_rate_limits()` leaves them on.

## pytest fixtures

Enable the plugin in your top-level `conftest.py`:

<!-- skip -->
```python
# conftest.py
pytest_plugins = ["python_wrap.testing"]
```

You get two fixtures:

- `pw_fake_clock`: installs a `FakeClock` for the test and yields it.
- `pw_fast_mode`: runs the test under `fast_mode()`.

<!-- skip -->
```python
def test_backoff(pw_fake_clock):
    assert flaky() == "ok"
    assert pw_fake_clock.sleeps == [1.0, 2.0, 4.0]

def test_quick(pw_fast_mode):
    ...
```

`python_wrap.testing` imports without pytest installed; the fixtures only exist when it is.

## Spies

`spy(func)` records every call without changing behaviour, for sync and async functions. `spy_on(obj, name)` patches a module, class or instance attribute for a block and restores it.

```python
import math
from python_wrap.testing import spy, spy_on

@spy
def add(a: int, b: int) -> int:
    return a + b

add(1, b=2)
assert add.call_count == 1 and add.called
add.assert_called_with(1, b=2)
call = add.calls[0]
assert (call.args, call.kwargs, call.result, call.exception) == ((1,), {"b": 2}, 3, None)
add.reset()
assert add.call_count == 0

with spy_on(math, "floor") as floor:
    math.floor(2.5)
assert floor.call_count == 1       # and math.floor is the original again
```

Each `Call` holds `args`, `kwargs`, `result`, `exception` and `duration`. On a class, a spy records `self` as the first argument.

## Waiting for background work

`eventually` (async) and `eventually_sync` retry an assertion until it passes or `timeout` runs out, then raise the last `AssertionError` annotated with how long it waited. They use real time unless a fake clock is installed.

```python
import asyncio
import threading
from python_wrap.testing import eventually, eventually_sync

done = threading.Event()
threading.Timer(0.02, done.set).start()

def finished() -> None:
    assert done.is_set()

eventually_sync(finished, timeout=2.0, interval=0.005)

async def main() -> None:
    state = {"ready": False}
    asyncio.get_running_loop().call_later(0.02, state.update, {"ready": True})

    async def check() -> None:        # sync or async assertions both work
        assert state["ready"]

    await eventually(check, timeout=2.0, interval=0.005)

asyncio.run(main())
```

!!! tip "Under `fast_mode()`"
    `eventually` waits through `_core`, so inside `fast_mode()` its polling interval is zero and it spins until `timeout`. Leave it outside fast mode when you wait on real threads.

## Patching the clock by hand

Everything above is built on four module attributes of `python_wrap._core`, always read as attributes so they can be patched:

| Attribute | Used for |
| --- | --- |
| `_core.monotonic()` | Rate-limit windows, throttle, cache TTL, circuit recovery, retry `elapsed` |
| `_core.perf_counter()` | `timing`, `benchmark`, `audit` durations |
| `_core.sleep(seconds)` | Waiting in sync `retry` and `rate_limit(block=True)` |
| `_core.async_sleep(seconds)` | The same, for `async def` functions |

If you prefer `unittest.mock`, patch them directly:

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
    assert flaky() == "ok"

assert sleeps == [1.0, 2.0, 4.0]
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
    With `mock`, patch `_core.async_sleep` with an `async def` that records and returns (as `FakeClock.async_sleep` does) so `async def` retries do not wait either.

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

## Bypass and assertions

`disabled()` combines single-attempt retry, cache bypass, and disabled rate limits.
It does not remove real deadlines, stop background workers, or bypass distributed leases.
The same helper is available as `python_wrap.disabled` and `python_wrap.testing.disabled`.

```python
from python_wrap import disabled, memoize, retry
from python_wrap.testing import assert_cache_hit, assert_retried

attempts = []

@retry(max_attempts=2, delay=0)
def flaky():
    attempts.append(1)
    if len(attempts) == 1:
        raise ValueError("temporary")
    return 7

assert flaky() == 7
assert_retried(flaky, times=1)       # extra attempts, not total attempts

@memoize
def cached():
    return 1

assert cached() == cached() == 1
assert_cache_hit(cached, times=1)
with disabled():
    assert cached() == 1
assert_cache_hit(cached, times=1)    # bypass did not increment cache hits
```

The assertions compare totals since the component's last reset, not a hidden baseline
for a particular call. Reset counters/caches between tests. `breaker.force_open()` can
exercise fallback behavior directly, and `breaker.reset()` restores normal operation.
