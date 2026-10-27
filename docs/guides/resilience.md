# Resilience

Calls to networks, disks and other services fail. These decorators decide what happens next: try again, return a default, give up quietly, stop calling a broken dependency, or stop waiting.

## `retry`

Call the function again when it raises. The last exception is re-raised unchanged, with its original traceback, once attempts run out.

=== "Sync"

    ```python
    from python_wrap import retry

    calls = []

    @retry(max_attempts=4, delay=0.01, backoff=2, on=ConnectionError)
    def fetch() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("down")
        return "payload"

    assert fetch() == "payload"
    assert len(calls) == 3
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import retry

    calls = []

    @retry(max_attempts=4, delay=0.01, backoff=2, on=ConnectionError)
    async def fetch() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("down")
        return "payload"

    assert asyncio.run(fetch()) == "payload"
    assert len(calls) == 3
    ```

The wait before attempt `n + 1` is `min(delay * backoff ** (n - 1), max_delay) + random.uniform(0, jitter)`.

| Option | Default | Meaning |
| --- | --- | --- |
| `max_attempts` | `3` | Total calls allowed. `None` retries forever. |
| `delay` | `1.0` | Base wait in seconds. |
| `backoff` | `1.0` | Multiplier applied after each failure (use `2` for exponential). |
| `max_delay` | `None` | Cap on the backoff wait (before jitter). |
| `jitter` | `0.0` | Up to this many random extra seconds, so many clients do not retry in lockstep. |
| `max_time` | `None` | Total time budget in seconds from the first attempt. |
| `on` | `Exception` | Exception type(s) that trigger a retry. Anything else propagates immediately. |
| `retry_if` | `None` | Extra predicate on the exception; retry only if it returns `True`. |
| `on_retry` | `None` | Called with a `RetryState` before each wait. |
| `delay_from` | `None` | `exc -> seconds or None`: exact wait for this attempt (e.g. `Retry-After`). |
| `logger` | package logger | Logger or logger name for the per-retry message. |
| `log_level` | `WARNING` | Level of the per-retry message; `None` disables it. |
| `log_exc_info` | `False` | Attach the traceback to the per-retry log record. |

`retry` works bare (`@retry`), with options, and as a plain call: `retry(func, max_attempts=5)`.

!!! warning "Retry only what is safe to repeat"
    Retrying a non-idempotent call (charging a card, sending an email) can do it twice. Narrow `on=` to errors that mean "it did not happen", and use `retry_if` to inspect the exception.

### Deciding what is retryable

```python
from python_wrap import RetryState, retry

class HttpError(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status

seen: list[RetryState] = []
responses = iter([503, 502, 200])

@retry(
    max_attempts=5,
    delay=0.01,
    on=HttpError,
    retry_if=lambda exc: exc.status >= 500,
    on_retry=seen.append,
)
def get() -> int:
    status = next(responses)
    if status != 200:
        raise HttpError(status)
    return status

assert get() == 200
assert [s.attempt for s in seen] == [1, 2]
assert isinstance(seen[0].exception, HttpError)
```

`RetryState` carries `attempt`, `exception`, `next_delay` and `elapsed`, which is enough to feed a metric or a custom log line.

### Honour `Retry-After`

Servers that answer 429 or 503 often say how long to wait. Pass `delay_from` a function that maps the exception to a number of seconds, or to `None` to fall back to the normal backoff. A number is the exact wait for that attempt (no backoff, no jitter), still capped by `max_delay` and `max_time`.

`retry_after_header` reads `exc.retry_after` or `exc.response.headers["Retry-After"]` (seconds or an HTTP-date) and never raises, so it works with `httpx.HTTPStatusError`, `requests.HTTPError` and similar without importing them. The example uses a stand-in exception with the same shape:

```python
from python_wrap import retry
from python_wrap.retry import retry_after_header

class Response:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers

class HTTPStatusError(Exception):  # shaped like httpx / requests errors
    def __init__(self, headers: dict[str, str]) -> None:
        super().__init__("429 Too Many Requests")
        self.response = Response(headers)

replies = iter([{"Retry-After": "0.01"}, {}, None])

@retry(on=HTTPStatusError, delay=0.01, delay_from=retry_after_header, max_delay=30)
def fetch() -> str:
    headers = next(replies)
    if headers is not None:
        raise HTTPStatusError(headers)
    return "payload"

assert fetch() == "payload"
```

Any function works as `delay_from`, e.g. `delay_from=lambda exc: getattr(exc, "wait", None)`. If it raises, the normal backoff is used.

### Time budget

`max_time` is a total budget in seconds, measured from the first attempt. `retry` never sleeps past it: if the next wait would end after the deadline, the last exception is re-raised at once.

```python
from python_wrap import retry

calls = []

@retry(max_attempts=None, delay=0.01, backoff=2, max_time=0.05)
def flaky() -> None:
    calls.append(1)
    raise ConnectionError("down")

try:
    flaky()
except ConnectionError:
    pass
assert len(calls) >= 2
```

`retry` also honours an enclosing [`deadline`](../api/deadline.md): it never sleeps past `deadline.remaining()`. If the next wait would outlast the deadline, or none is left, the last exception is re-raised at once (the same rule as `max_time`).

```python
from python_wrap import retry
from python_wrap.deadline import deadline

calls = []

@retry(max_attempts=None, delay=0.02)
def flaky() -> None:
    calls.append(1)
    raise ConnectionError("down")

with deadline(0.05):
    try:
        flaky()
    except ConnectionError:
        pass
assert 1 <= len(calls) <= 4
```

### Project-wide defaults

Set `max_attempts`, `delay`, `backoff`, `max_delay`, `jitter`, `max_time` and `log_level` once for the whole project with `python_wrap.config.configure(retry={...})`. See [Project-wide configuration](configuration.md).

### Cancellation and shutdown

`asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` are **never retried**, even with `on=BaseException` or when you list them in `on`: retrying them would make tasks and processes impossible to stop. Cancelling an async call while it waits between attempts cancels it immediately.

### Logging

Each retry logs one message at `log_level` (default `WARNING`) on `logger`. Use `log_level=logging.INFO` to quiet it, `log_level=None` to turn it off, and `log_exc_info=True` to attach the traceback.

### Statistics

`retry_stats(func)` returns a `RetryStats` with thread-safe counters: `calls`, `attempts_total`, `retries`, `gave_up`, `total_sleep` and `last_exception`. Counters are per decorated function (shared by all instances for methods); `snapshot()` copies them and `reset()` clears them.

```python
from python_wrap import retry
from python_wrap.retry import retry_stats

n = []

@retry(delay=0.01)
def fetch() -> str:
    n.append(1)
    if len(n) < 3:
        raise OSError("flaky")
    return "ok"

assert fetch() == "ok"
stats = retry_stats(fetch)
assert stats is not None
assert (stats.calls, stats.attempts_total, stats.retries, stats.gave_up) == (1, 3, 2, 0)
```

### Retrying a block of code

`retrying(...)` takes the same options as `retry` and lets you retry code that is not a function of its own. Put the risky part in `with attempt:`; a retryable exception is swallowed and the loop waits and continues, success ends the loop, and the last failure propagates. `attempt.number` is the 1-based attempt number.

=== "Sync"

    ```python
    from python_wrap.retry import retrying

    tries = []

    for attempt in retrying(max_attempts=3, delay=0.01, on=ConnectionError):
        with attempt:
            tries.append(attempt.number)
            if len(tries) < 3:
                raise ConnectionError("down")

    assert tries == [1, 2, 3]
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap.retry import retrying

    async def main() -> list[int]:
        tries: list[int] = []
        async for attempt in retrying(max_attempts=3, delay=0.01, on=ConnectionError):
            with attempt:
                tries.append(attempt.number)
                if len(tries) < 3:
                    raise ConnectionError("down")
        return tries

    assert asyncio.run(main()) == [1, 2, 3]
    ```

Use `async for` inside coroutines: a plain `for` would block the event loop while waiting.

### Metrics and tests

`retry` emits the events `retry.attempt_failed` (`attempt`, `exception`, `next_delay`), `retry.succeeded` (`attempts`) and `retry.gave_up` (`attempts`, `exception`) for `python_wrap.events` listeners. In tests, the `max_attempts` override from `python_wrap.testing` caps the attempts of every `retry` and `retrying` at call time.

## `fallback`

Return a value, or the result of a handler, when the function raises.

=== "Sync"

    ```python
    from python_wrap import fallback

    @fallback(0, on=KeyError)
    def stock(sku: str) -> int:
        return {"a": 5}[sku]

    assert stock("a") == 5
    assert stock("missing") == 0

    @fallback(handler=lambda exc: f"failed: {exc!r}")
    def risky() -> str:
        raise RuntimeError("boom")

    assert risky() == "failed: RuntimeError('boom')"
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import fallback

    @fallback(0, on=KeyError)
    async def stock(sku: str) -> int:
        return {"a": 5}[sku]

    assert asyncio.run(stock("missing")) == 0
    ```

Give exactly one of `value` or `handler=`; giving both or neither raises `ValueError`. The exception is logged at WARNING with its traceback. For async functions the handler is still a regular function.

!!! tip
    Pass a fresh object with `handler=lambda exc: []` if the default is mutable, so callers do not share one list.

## `suppress`

Swallow matching exceptions; the call returns `None`. Suppressed errors are logged at DEBUG.

```python
from python_wrap import suppress

@suppress(KeyError)
def first(d: dict[str, int]) -> int:
    return d["x"]

assert first({"x": 1}) == 1
assert first({}) is None

@suppress  # bare form suppresses Exception
def best_effort() -> None:
    raise RuntimeError

assert best_effort() is None
```

Use `fallback` when you need a value other than `None`. Use `suppress` for best-effort work such as cleanup or telemetry.

## Circuit breaker

When a dependency is down, retrying makes things worse. A circuit breaker counts consecutive failures and, past a threshold, rejects calls immediately with `CircuitOpenError` for a while.

| State | Behaviour | Moves on when |
| --- | --- | --- |
| `closed` | Calls run normally. | `failure_threshold` consecutive failures: becomes `open`. |
| `open` | Calls are rejected with `CircuitOpenError`. | `recovery_timeout` seconds pass: becomes `half_open`. |
| `half_open` | A limited number of trial calls run. | A trial succeeds: `closed`. A trial fails: `open` again. |

```python
import time
from python_wrap import CircuitOpenError, CircuitState, circuit_breaker

@circuit_breaker(failure_threshold=2, recovery_timeout=0.02, on=ConnectionError)
def call_api() -> str:
    raise ConnectionError("down")

for _ in range(2):
    try:
        call_api()
    except ConnectionError:
        pass

assert call_api.breaker.state is CircuitState.OPEN
try:
    call_api()
except CircuitOpenError as exc:
    assert exc.retry_after <= 0.02

time.sleep(0.03)
assert call_api.breaker.state is CircuitState.HALF_OPEN
call_api.breaker.reset()
assert call_api.breaker.state is CircuitState.CLOSED
```

`@circuit_breaker(...)` gives the function its own breaker, exposed as `wrapper.breaker`. To **share one breaker across several functions** (they all hit the same service), create a `CircuitBreaker` and use the instance as the decorator:

```python
from python_wrap import CircuitBreaker, CircuitOpenError

payments = CircuitBreaker(failure_threshold=1, recovery_timeout=60, name="payments")

@payments
def charge() -> None:
    raise RuntimeError("provider down")

@payments
def refund() -> None:
    ...

try:
    charge()
except RuntimeError:
    pass

try:
    refund()  # never runs: the shared circuit is open
except CircuitOpenError:
    pass
else:
    raise AssertionError("expected the circuit to be open")

assert payments.failure_count == 1
```

You can also call through a breaker without decorating: `breaker.call(func, *args, **kwargs)` (for an `async def` function it returns a coroutine to await).

Only exceptions matching `on=` count as failures; others pass through uncounted. `half_open_max_calls` limits how many trial calls may run at once while half-open.

### Tuning recovery, exclusions and monitoring

| Option | Default | What it does |
| --- | --- | --- |
| `success_threshold` | `1` | Consecutive successful trial calls needed to close a half-open circuit. Any counted failure during the trials re-opens it. |
| `exclude` | `()` | Exception types that are never counted, even when they match `on=` (for example a "not found" error that proves the service is up). They propagate unchanged. |
| `on_state_change` | `None` | `listener(breaker, old_state, new_state)`, called after every transition, outside the breaker's lock. An exception from the listener is logged, never raised. |
| `name` | function name | Used in log messages, `CircuitOpenError` messages and events. |

`breaker.stats` returns a frozen `CircuitStats` snapshot: `state`, total `failures`, `successes` and `rejected` calls (since creation or the last `reset()`), `consecutive_failures`, and `opened_at` (the `monotonic()` time the circuit last opened, `None` while closed).

```python
from python_wrap import CircuitBreaker, CircuitState

class NotFound(Exception):
    pass

changes = []
inventory = CircuitBreaker(
    failure_threshold=2,
    recovery_timeout=0,          # half-open again straight away, for the example
    success_threshold=2,         # two good trial calls before trusting it again
    exclude=NotFound,            # a 404 means the service is up
    on_state_change=lambda b, old, new: changes.append(new.value),
    name="inventory",
)

@inventory
def lookup(sku: str) -> int:
    if sku == "missing":
        raise NotFound(sku)
    if sku == "broken":
        raise ConnectionError("down")
    return 3

for sku in ("missing", "broken", "broken"):
    try:
        lookup(sku)
    except (NotFound, ConnectionError):
        pass

assert inventory.stats.failures == 2   # NotFound was not counted
assert lookup("ok") == 3               # first trial: still half-open
assert inventory.state is CircuitState.HALF_OPEN
assert lookup("ok") == 3               # second trial closes it
assert changes == ["open", "half_open", "closed"]
```

The breaker also emits the `circuit.state_changed` (`old`, `new`) and `circuit.rejected` (`retry_after`) [events](observability.md), reported under the breaker's name.

## `timeout`

Raise `TimeoutExpired` (a subclass of the built-in `TimeoutError`) if a call runs too long.

=== "Sync"

    ```python
    import time
    from python_wrap import TimeoutExpired, timeout

    @timeout(0.02)
    def slow() -> None:
        time.sleep(1)

    try:
        slow()
    except TimeoutExpired as exc:
        assert exc.seconds == 0.02
        assert isinstance(exc, TimeoutError)
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import TimeoutExpired, timeout

    @timeout(0.02)
    async def slow() -> None:
        await asyncio.sleep(1)

    async def main() -> None:
        try:
            await slow()
        except TimeoutExpired:
            pass
        else:
            raise AssertionError("expected a timeout")

    asyncio.run(main())
    ```

How the deadline is enforced depends on the function and the platform:

| Situation | Strategy | Stops the work? |
| --- | --- | --- |
| `async def` | `asyncio.timeout` (3.11+) or a cancellation timer (3.10) cancels the coroutine | Yes |
| Sync, Unix, main thread | `SIGALRM` timer interrupts the call | Yes |
| Sync on Windows, in a non-main thread, or `use_signals=False` | Runs in a daemon worker thread; the caller stops waiting | **No** |

!!! warning "The thread fallback cannot stop the work"
    When signals are unavailable, the function keeps running in the background after the timeout, and its result is discarded. Do not rely on it to cancel side effects. See [Limitations](../limitations.md#timeout).

Nested timeouts work: the previous `SIGALRM` handler and any already-armed timer are restored afterwards. `use_signals=True` forces signals (and raises `RuntimeError` where unavailable); `use_signals=False` forces the thread strategy.

Every expiry emits the `timeout.expired` (`seconds`) [event](observability.md).

### Timing out a block

The object `timeout(...)` returns is also a context manager, so you can limit a block of code without wrapping it in a function. The same object still works as a decorator.

=== "Async"

    ```python
    import asyncio
    from python_wrap import TimeoutExpired, timeout

    async def main() -> None:
        try:
            async with timeout(0.02):
                await asyncio.sleep(1)
        except TimeoutExpired as exc:
            assert exc.seconds == 0.02
        else:
            raise AssertionError("expected a timeout")

    asyncio.run(main())
    ```

=== "Sync"

    <!-- unix-only -->
    ```python
    import time
    from python_wrap import TimeoutExpired, timeout

    try:
        with timeout(0.02):   # Unix, main thread only
            time.sleep(1)
    except TimeoutExpired:
        pass
    ```

`async with timeout(...)` uses `asyncio.timeout` on Python 3.11+ and a cancellation timer on 3.10; as with the decorator, a `TimeoutError` raised by your own code inside the block propagates unchanged.

The sync `with timeout(...)` form only exists on top of `SIGALRM`: it needs Unix and the main thread. Elsewhere (Windows, worker threads, `use_signals=False`) entering it raises `RuntimeError`, because a block of code cannot be moved to a worker thread. Decorate a function instead, which falls back to the thread strategy.

### Deadlines: one budget for many steps

A `timeout` limits one call. A **deadline** limits everything inside a block — several calls, retries, sub-steps — to one shared budget. `deadline(seconds)` stores an absolute expiry in a context variable, so it is visible to every function called inside the block, sync or async, and to tasks started from it. Nested deadlines never extend an outer one: the earliest wins.

A deadline does not interrupt anything by itself. Instead:

* `timeout` (decorator or block) clamps its limit to the time the deadline has left. If the deadline has already passed, the call is not started and `TimeoutExpired` is raised at once.
* `remaining()` returns the seconds left (or `None` outside any deadline). Pass it on, for example as an HTTP client's timeout, or use it to cap your own retry loop.
* `check()` raises `TimeoutExpired` once the deadline has passed. Call it between steps of a long job.

```python
import asyncio
from python_wrap import TimeoutExpired, timeout
from python_wrap.deadline import check, deadline, remaining

@timeout(30)                     # generous per-call limit...
async def fetch(part: int) -> int:
    await asyncio.sleep(0.01 if part < 2 else 1)
    return part

async def handle_request() -> list[int]:
    results = []
    async with deadline(0.1):    # ...but the whole request gets 0.1s
        for part in range(5):
            check()              # stop early once the budget is spent
            left = remaining()
            assert left is not None and left <= 0.1
            results.append(await fetch(part))   # clamped to what is left
    return results

async def main() -> None:
    try:
        await handle_request()
    except TimeoutExpired as exc:
        assert exc.seconds <= 0.1
    else:
        raise AssertionError("expected the deadline to run out")

asyncio.run(main())
```
