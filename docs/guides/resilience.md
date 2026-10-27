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
| `on` | `Exception` | Exception type(s) that trigger a retry. Anything else propagates immediately. |
| `retry_if` | `None` | Extra predicate on the exception; retry only if it returns `True`. |
| `on_retry` | `None` | Called with a `RetryState` before each wait. |
| `logger` | package logger | Logger or logger name for the per-retry WARNING. |

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
| `async def` | `asyncio.wait_for` cancels the coroutine | Yes |
| Sync, Unix, main thread | `SIGALRM` timer interrupts the call | Yes |
| Sync on Windows, in a non-main thread, or `use_signals=False` | Runs in a daemon worker thread; the caller stops waiting | **No** |

!!! warning "The thread fallback cannot stop the work"
    When signals are unavailable, the function keeps running in the background after the timeout, and its result is discarded. Do not rely on it to cancel side effects. See [Limitations](../limitations.md#timeout).

Nested timeouts work: the previous `SIGALRM` handler and any already-armed timer are restored afterwards. `use_signals=True` forces signals (and raises `RuntimeError` where unavailable); `use_signals=False` forces the thread strategy.
