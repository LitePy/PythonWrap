# Retry

`retry` calls the function again when it raises, so transient failures (a dropped connection, a 503) do not reach your caller. The last exception is re-raised unchanged, with its original traceback, once attempts run out.

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

`retry` works bare (`@retry`), with options, and as a plain call: `retry(func, max_attempts=5)`.

## When to use it

- Errors that are likely to disappear on their own: dropped connections, 429 and 5xx responses, lock contention.
- Operations that are safe to repeat.
- Not for a dependency that stays down: add a [circuit breaker](circuit-breaker.md) so retries stop piling onto it.
- Not for a call that is slow rather than failing: use [timeout](timeout.md), or [hedge](hedge.md) for tail latency.

!!! warning "Retry only what is safe to repeat"
    Retrying a non-idempotent call (charging a card, sending an email) can do it twice. Narrow `on=` to errors that mean "it did not happen", and use `retry_if` to inspect the exception.

## Options

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

## Choosing what to retry

`on` picks exception types; `retry_if` looks inside the exception. `on_retry` receives a `RetryState` with `attempt`, `exception`, `next_delay` and `elapsed`, which is enough to feed a metric or a custom log line.

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

## Honouring Retry-After

Servers that answer 429 or 503 often say how long to wait. Pass `delay_from` a function that maps the exception to a number of seconds, or to `None` to fall back to the normal backoff. A number is the exact wait for that attempt (no backoff, no jitter), still capped by `max_delay` and `max_time`.

`retry_after_header` reads `exc.retry_after` or `exc.response.headers["Retry-After"]` (seconds or an HTTP-date) and never raises, so it works with `httpx.HTTPStatusError`, `requests.HTTPError` and similar without importing them. The example uses a stand-in exception with the same shape:

```python
from python_wrap import retry
from python_wrap import retry_after_header

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

## Time budget

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

`retry` also honours an enclosing [`deadline`](timeout.md#deadlines-one-budget-for-many-steps): it never sleeps past `remaining()` (from `python_wrap.deadline`). If the next wait would outlast the deadline, or none is left, the last exception is re-raised at once (the same rule as `max_time`).

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

## Retrying a block of code

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

## Statistics

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

## Project-wide defaults

Set `max_attempts`, `delay`, `backoff`, `max_delay`, `jitter`, `max_time` and `log_level` once for the whole project with `python_wrap.config.configure(retry={...})`. See [Configuration](../configuration.md).

## Logging

Each retry logs one message at `log_level` (default `WARNING`) on `logger`. Use `log_level=logging.INFO` to quiet it, `log_level=None` to turn it off, and `log_exc_info=True` to attach the traceback.

## Events

| Event | Fields |
| --- | --- |
| `retry.attempt_failed` | `attempt` (1-based), `exception`, `next_delay` |
| `retry.succeeded` | `attempts` |
| `retry.gave_up` | `attempts`, `exception` |

Subscribe with `python_wrap.events`; see [Metrics](../observability/metrics.md).

## Testing

`fast_mode()` makes the backoff waits instant, and `fake_clock()` turns them into clock advances you can assert on. `disable_retries()` makes `retry` a single attempt, and the `max_attempts` override from `python_wrap.testing` caps the attempts of every `retry` and `retrying` at call time. See [Testing](../../testing/index.md).

## Gotchas

!!! warning "Cancellation and shutdown are never retried"
    `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` are **never retried**, even with `on=BaseException` or when you list them in `on`: retrying them would make tasks and processes impossible to stop. Cancelling an async call while it waits between attempts cancels it immediately.

!!! note "`python_wrap.retry` is the function"
    The package attribute `python_wrap.retry` is the decorator, not the module. Import helpers explicitly: `from python_wrap.retry import retrying, retry_stats`.

## See also

- [Circuit breaker](circuit-breaker.md), [Timeout and deadline](timeout.md), [Policies](policies.md)
- [Stacking](../../get-started/stacking.md)
- [API reference](../../api/retry.md)
