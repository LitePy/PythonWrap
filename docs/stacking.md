# Stacking decorators

Decorators apply bottom to top: the one nearest the `def` wraps the function first, and the one at the top is the **outermost** layer, the first to see a call and the last to see the result.

```python
from python_wrap import retry, timeout

@retry(max_attempts=3, delay=0.01)   # outer: sees the final result of all attempts
@timeout(5)                          # inner: bounds each single attempt
def fetch() -> str:
    return "ok"
```

Order changes behaviour, not just style. A good rule: **put a decorator outside whatever it should observe or protect as a whole, and inside whatever should happen once per attempt.**

## Order matters: four examples

### `timing` above or below `retry`

`timing` above `retry` times the whole call, including every attempt and every wait. Below it, `timing` measures each attempt separately.

```python
from python_wrap import TimingRecord, retry, timing

outer: list[TimingRecord] = []
inner: list[TimingRecord] = []

@timing(callback=outer.append)                    # one record for the whole call
@retry(max_attempts=3, delay=0.01, on=ValueError)
@timing(callback=inner.append)                    # one record per attempt
def flaky() -> None:
    raise ValueError

try:
    flaky()
except ValueError:
    pass

assert len(outer) == 1 and outer[0].success is False
assert len(inner) == 3
assert outer[0].elapsed >= 0.02                   # includes the two 10 ms waits
```

### `timeout` above or below `retry`

`retry` above `timeout`: each attempt gets its own deadline. `timeout` above `retry`: one deadline covers all attempts and the waits between them.

=== "Per attempt (retry above)"

    ```python
    import time
    from python_wrap import TimeoutExpired, retry, timeout

    attempts = []

    @retry(max_attempts=3, delay=0.001, on=TimeoutExpired)
    @timeout(0.01)
    def slow() -> None:
        attempts.append(1)
        time.sleep(1)

    try:
        slow()
    except TimeoutExpired:
        pass

    assert len(attempts) == 3      # three separate 10 ms deadlines
    ```

=== "Overall (timeout above)"

    ```python
    import time
    from python_wrap import TimeoutExpired, retry, timeout

    attempts = []

    @timeout(0.03)
    @retry(max_attempts=100, delay=0.01, on=ConnectionError)
    def always_fails() -> None:
        attempts.append(1)
        raise ConnectionError

    try:
        always_fails()
    except TimeoutExpired:
        pass

    assert 1 <= len(attempts) < 100   # cut off by the overall deadline
    ```

!!! warning "Narrow `on=` when an outer `timeout` wraps a `retry`"
    `TimeoutExpired` is an ordinary exception. A `retry` left at its default `on=Exception` can catch the timeout from an outer `timeout` and keep going, so the overall deadline is swallowed. Name the transient errors you want retried (`on=ConnectionError`) and the timeout passes through.

### `circuit_breaker` above or below `retry`

Above `retry`, the breaker counts one failure per call whose retries were all used up. Below it, every single attempt counts, so one unlucky call can trip the breaker, and `retry` then sees `CircuitOpenError`.

```python
from python_wrap import CircuitOpenError, circuit_breaker, retry

@circuit_breaker(failure_threshold=2, recovery_timeout=60, on=ConnectionError)
@retry(max_attempts=3, delay=0, on=ConnectionError)
def call() -> None:
    raise ConnectionError

for _ in range(2):
    try:
        call()
    except ConnectionError:
        pass

assert call.breaker.failure_count == 2     # two calls, six attempts

try:
    call()
except CircuitOpenError:
    pass
```

Usually you want the breaker **above** `retry`: "this dependency has failed N calls in a row, after retrying".

### `rate_limit` above or below `retry`

Above `retry`: one count per logical call. Below it: every attempt counts against the limit, which is right when the limit is the remote service's quota and wrong when it protects your own callers.

## A recommended order

Outermost first. Skip the layers you do not need.

| # | Decorator | Why here |
| --- | --- | --- |
| 1 | `deprecate`, `mock_data` | Warn or short-circuit before anything else runs. |
| 2 | `audit`, `log_args`, `log_return`, `timing` | Observe the whole call, including retries and fallbacks. |
| 3 | `validate_args`, `check_type`, `enforce_types` | Reject bad input immediately; do not retry or cache it. |
| 4 | `fallback`, `suppress` | Catch whatever the resilience layers below could not fix. |
| 5 | `memoize`, `once` | A cache hit skips everything below it. Exceptions are not cached. |
| 6 | `singleflight` | Concurrent cache misses share one execution. |
| 7 | `rate_limit`, `throttle`, `debounce` | Cap calls per logical request. |
| 8 | `circuit_breaker` | Counts calls whose retries were exhausted. |
| 9 | `retry` | Repeats everything below it. |
| 10 | `timeout` | A deadline per attempt. |
| 11 | `revert_on_failure`, `transactional` | Each attempt starts from clean state and undoes itself. |
| 12 | `timing` (optional second one) | Per-attempt durations. |

```python
from python_wrap import (
    circuit_breaker, enforce_types, fallback, memoize, retry, timeout, timing,
)

@timing(threshold=1.0)
@enforce_types
@fallback({}, on=ConnectionError)
@memoize(ttl=30)
@circuit_breaker(failure_threshold=5, recovery_timeout=30, on=ConnectionError)
@retry(max_attempts=3, delay=0.2, backoff=2, on=ConnectionError)
@timeout(5)
def load_profile(user_id: int) -> dict:
    ...
```

Put the same stack on many functions with [`compose`](guides/devtools.md#compose).

## Rules of thumb

- **`retry(on=...)` should name transient errors.** With the default `on=Exception` it also retries `ValidationError`, `CircuitOpenError` and `TimeoutExpired`, which rarely helps.
- **Observers go outside, guards go inside.** An `audit` or `timing` outside the retry loop records the outcome the caller experienced.
- **Caches go outside the things that are expensive to repeat.** `memoize` above `retry` caches the final success and skips the retry loop on a hit.
- **`singleflight` goes below `memoize`.** Cache first; only a miss needs de-duplicating.
- **Decorators that hold state belong to the function.** Stacking never merges state, but see [shared state on methods](limitations.md#shared-state-on-methods).
- **Async and sync stack the same way.** Every decorator preserves whether the function is `async def`, so the order rules are identical. See [Async support](async.md).
