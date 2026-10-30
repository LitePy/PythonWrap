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
| `delays` | `None` | Explicit nonempty delay sequence; repeat the last entry. Overrides `delay`/`backoff`, with the same cap and jitter. |
| `backoff` | `1.0` | Multiplier applied after each failure (use `2` for exponential). |
| `max_delay` | `None` | Cap on the backoff wait (before jitter). |
| `jitter` | `0.0` | Up to this many random extra seconds, so many clients do not retry in lockstep. |
| `max_time` | `None` | Total time budget in seconds from the first attempt. |
| `on` | `Exception` | Exception type(s) that trigger a retry. Anything else propagates immediately. |
| `retry_if` | `None` | Extra predicate on the exception; retry only if it returns `True`. May be `async def` for async functions. |
| `retry_if_result` | `None` | Predicate on the return value; `True` means "retry". The last value is returned when retries run out. May be `async def` for async functions. |
| `on_retry` | `None` | Called with a `RetryState` before each wait. May be async for async functions. Ordinary hook errors are logged and swallowed. |
| `delay_from` | `None` | Exception or rejected result → seconds or None: exact wait for this attempt (e.g. `Retry-After`). |
| `budget` | `None` | A `RetryBudget` shared with other retried code, so retries stay a fraction of traffic. |
| `logger` | package logger | Logger or logger name for the per-retry message. |
| `log_level` | `WARNING` | Level of the per-retry message; `None` disables it. |
| `log_values` | `True` | Set `False` for exception/result type names only, including hint/hook failures. Also suppresses traceback logging. |
| `log_exc_info` | `False` | Attach the traceback to the per-retry log record. |

## Job-specific delay schedules

Use `delays=[5, 30, 120]` when the job contract specifies exact waits instead of an
exponential curve. With `max_attempts=3`, waits are **5 and 30**: there is no wait
after the final failure. More failures repeat the last entry. Each invocation starts
from the first entry, and the sequence is copied when the decorator or iterator is
created. Values must be finite nonnegative seconds; use `[0]` for immediate retries.
`retrying` accepts the same option.

A valid `delay_from` hint takes priority over the schedule, without jitter. Otherwise
`max_delay` caps the scheduled wait before adding `jitter`. All attempt/time/deadline
and retry-budget limits still apply.

## Logging around credentials

Use `log_values=False` when exception messages or response representations can contain
credentials, URLs with tokens, or customer data. Retry messages then include only the
exception/result type. Hint/hook failures also omit messages and tracebacks, even if
`log_exc_info=True`. The default remains compatible with existing logging.

This controls the retry engine's own logs. Stats, events, predicates and callbacks still
receive the original objects. Keep custom callbacks, function names, event subscribers,
and the application's final exception handler free of secrets too.

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

## Retrying on a returned value

Some APIs report "not ready yet" in the return value instead of raising: a job status of `"pending"`, an empty page, `None`. `retry_if_result` receives each returned value; when it returns `True` the attempt counts as failed and is retried with the usual backoff.

```python
from python_wrap import retry

statuses = iter(["pending", "pending", "done"])

@retry(max_attempts=5, delay=0.01, retry_if_result=lambda status: status == "pending")
def job_status() -> str:
    return next(statuses)

assert job_status() == "done"
```

When no retry is left (attempts, `max_time`, deadline or budget), the **last value is returned**, not an exception: the function did not fail, it just never gave the answer you hoped for. Check the value if it matters:

```python
from python_wrap import retry

@retry(max_attempts=3, delay=0.01, retry_if_result=lambda status: status == "pending")
def job_status() -> str:
    return "pending"

assert job_status() == "pending"  # three attempts, then the last value
```

`retry_if_result` combines with `on`/`retry_if`: exceptions and bad values share the same attempt count. In the `RetryState` passed to `on_retry`, `exception` is `None` and `result` holds the rejected value; the `retry.attempt_failed` and `retry.gave_up` events carry `exception=None`. An exception raised by the predicate itself propagates at once.

!!! note "Why not raise?"
    tenacity raises `RetryError` when the result predicate never passes; the `backoff` library returns the last value. python-wrap returns it, so the decorated function keeps its return type and you never have to unwrap a wrapper exception. If you prefer an error, raise one yourself in the function instead of using `retry_if_result`.

## Async hooks

For `async def` functions, `retry_if`, `retry_if_result` and `on_retry` may be `async def` callables: they are awaited, so they can look something up or report without blocking the loop. Plain functions keep working.

```python
import asyncio
from python_wrap import RetryState, retry

reported: list[int] = []

async def is_transient(exc: BaseException) -> bool:
    await asyncio.sleep(0)  # e.g. ask a feature flag service
    return isinstance(exc, ConnectionError)

async def report(state: RetryState) -> None:
    await asyncio.sleep(0)  # e.g. send a metric
    reported.append(state.attempt)

errors = iter([ConnectionError("down")])

@retry(max_attempts=3, delay=0.01, retry_if=is_transient, on_retry=report)
async def fetch() -> str:
    err = next(errors, None)
    if err is not None:
        raise err
    return "payload"

assert asyncio.run(fetch()) == "payload"
assert reported == [1]
```

Nothing can await an async hook around a plain function, so decorating a sync function with one raises `TypeError` straight away:

```python
from python_wrap import retry

async def is_transient(exc: BaseException) -> bool:
    return True

try:
    @retry(retry_if=is_transient)
    def fetch() -> str:
        return "payload"
except TypeError as exc:
    assert "retry_if= is an async function" in str(exc)
```

## Retry budgets

`max_attempts=3` on every call looks harmless until the dependency goes down: every request now makes three, and the retries keep it down. A `RetryBudget` caps retries at a fraction of the traffic, shared by every function that uses it (the Finagle and Envoy approach):

- every call's first attempt **deposits** into the budget, every retry **withdraws**;
- a retry is allowed while the retries in the last `window` seconds stay within `ratio` times the requests in that window, plus `min_per_second * window` so low-traffic code can still retry;
- when the budget refuses, `retry` stops at once: no sleep, the last exception is re-raised (or the last value returned with `retry_if_result`), and `retry.budget_exhausted` is emitted.

```python
from python_wrap import retry
from python_wrap.retry import RetryBudget, retry_stats

# Retries may add 10 % to traffic, and at least 1 retry per second is always allowed.
budget = RetryBudget(ratio=0.1, min_per_second=1, window=10)
calls = []

@retry(max_attempts=5, delay=0, on=ConnectionError, budget=budget)
def get_user() -> dict[str, str]:
    calls.append(1)
    raise ConnectionError("users service is down")

@retry(max_attempts=5, delay=0, on=ConnectionError, budget=budget)
def get_orders() -> list[str]:
    calls.append(1)
    raise ConnectionError("orders service is down")

for _ in range(10):
    for func in (get_user, get_orders):
        try:
            func()
        except ConnectionError:
            pass

# 20 calls; without the budget they would have made 100 attempts.
assert len(calls) == 20 + budget.stats.retries
assert budget.stats.retries <= 0.1 * 20 + 1 * 10
stats = retry_stats(get_user)
assert stats is not None and stats.budget_exhausted > 0
```

| Argument | Default | Meaning |
| --- | --- | --- |
| `ratio` | `0.1` | Retries allowed per request in the window. |
| `min_per_second` | `10` | Retries per second always allowed, whatever the traffic. |
| `window` | `10.0` | Length of the sliding window in seconds. |

`budget.stats` returns a `RetryBudgetStats` with `requests` and `retries` (in the current window), `rejected` (refused since creation or the last `reset()`) and `allowed` (retries it would grant right now). One budget is thread-safe and can be shared across threads, event loops, `retry` decorators and `retrying` blocks; it never blocks or sleeps. To give every retry in the project the same budget, set it once with `configure(retry={"budget": RetryBudget()})` before decorating; pass `budget=None` to opt a function out.

!!! tip "One budget per dependency"
    Share a budget between the calls that hit the same service. A budget shared by unrelated services lets a failing one spend the retries the others need.

## Honouring Retry-After

Servers that answer 429 or 503 often say how long to wait. Pass `delay_from` a synchronous function that maps the exception or rejected result to a number of seconds, or to `None` to fall back to the normal backoff. A number is the exact wait for that attempt (no backoff, no jitter), still capped by `max_delay` and `max_time`.

`retry_after_header` reads `.retry_after` or a `Retry-After` header on the object itself or its `.response` (seconds or an HTTP-date). It never raises, so it works with returned responses and `httpx.HTTPStatusError`/`requests.HTTPError` without importing those clients. The example uses a stand-in exception with the same shape:

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

A callback that returns None or raises uses normal backoff. An exception-only callback
should check the input type now that result retries can pass a response or any other
rejected value, including None.

### Returned responses

`http_status` matches `.status_code` or `.status`, so HTTP clients need not raise just
to retry a 429/503 response:

```python
from types import SimpleNamespace
from python_wrap import http_status, retry, retry_after_header
from python_wrap.testing import fake_clock

responses = iter([
    SimpleNamespace(status_code=429, headers={"Retry-After": "7"}),
    SimpleNamespace(status_code=200, headers={}),
])

@retry(retry_if_result=http_status(429, 503), delay_from=retry_after_header,
       max_attempts=2, delay=1)
def request():
    return next(responses)

with fake_clock() as clock:
    assert request().status_code == 200
assert clock.sleeps == [7.0]
```

For APIs with a body field, use
`delay_from=lambda outcome: retry_after_header(outcome, json_key="retry_after")`.
JSON decoding must be synchronous; this sync hint helper cannot await an async
response decoder. Hints still respect `max_delay`, `max_time`, and a surrounding deadline.

An ordinary `on_retry` exception is logged and swallowed. This prevents a failed
bookkeeping hook from replacing the original failure or rejected result. Cancellation,
KeyboardInterrupt and SystemExit still propagate. Use `retry_if` to decide whether a
failure is retryable, rather than intentionally raising from a notification hook.

`CircuitOpenError` is never retried in 0.3, even with a broad `on=` filter.
See [named policies and composable predicates](named-policies.md) for driver-error rules.

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

Use `async for` inside coroutines: a plain `for` would block the event loop while waiting. With async hooks (`retry_if`, `retry_if_result` or `on_retry` defined with `async def`), also write `async with attempt:` so they can be awaited; a plain `with attempt:` raises `TypeError` for them.

For `retry_if_result`, store the block's value in `attempt.result`. A rejected value starts another attempt; when no retry is left the loop ends normally and your variable holds the last value:

```python
from python_wrap.retry import retrying

pages = iter([[], [], ["row"]])
rows: list[str] = []

for attempt in retrying(max_attempts=5, delay=0.01, retry_if_result=lambda r: not r):
    with attempt:
        rows = next(pages)
        attempt.result = rows

assert rows == ["row"]
```

## Statistics

`retry_stats(func)` returns a `RetryStats` with thread-safe counters: `calls`, `attempts_total`, `retries`, `gave_up` (including calls that returned a rejected value), `budget_exhausted` (the part of `gave_up` caused by a `RetryBudget`), `total_sleep` and `last_exception`. Counters are per decorated function (shared by all instances for methods); `snapshot()` copies them and `reset()` clears them.

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

Set `max_attempts`, `delay`, `backoff`, `max_delay`, `jitter`, `max_time`, `log_level` and `budget` once for the whole project with `python_wrap.config.configure(retry={...})`. See [Configuration](../configuration.md).

## Logging

Each retry logs one message at `log_level` (default `WARNING`) on `logger`. Use `log_level=logging.INFO` to quiet it, `log_level=None` to turn it off, and `log_exc_info=True` to attach the traceback.

## Events

| Event | Fields |
| --- | --- |
| `retry.attempt_failed` | `attempt` (1-based), `exception` (`None` for a rejected result), `next_delay` |
| `retry.succeeded` | `attempts` |
| `retry.gave_up` | `attempts`, `exception` (`None` for a rejected result) |
| `retry.budget_exhausted` | `attempts`, `exception`; emitted just before `retry.gave_up` when a `RetryBudget` refuses |

Subscribe with `python_wrap.events`; see [Metrics](../observability/metrics.md).

## Testing

`fast_mode()` makes the backoff waits instant, and `fake_clock()` turns them into clock advances you can assert on. A `RetryBudget` reads time from the same clock, so `fake_clock.advance()` slides its window. `disable_retries()` makes `retry` a single attempt (a budget still receives its per-call deposit, but nothing is withdrawn because no retry happens), and the `max_attempts` override from `python_wrap.testing` caps the attempts of every `retry` and `retrying` at call time. See [Testing](../../testing/index.md).

## Gotchas

!!! warning "Cancellation and shutdown are never retried"
    `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` are **never retried**, even with `on=BaseException` or when you list them in `on`: retrying them would make tasks and processes impossible to stop. Cancelling an async call while it waits between attempts cancels it immediately.

!!! note "`python_wrap.retry` is the function"
    The package attribute `python_wrap.retry` is the decorator, not the module. Import helpers explicitly: `from python_wrap.retry import retrying, retry_stats`.

## See also

- [Circuit breaker](circuit-breaker.md), [Timeout and deadline](timeout.md), [Policies](policies.md)
- [Stacking](../../get-started/stacking.md)
- [API reference](../../api/retry.md)
