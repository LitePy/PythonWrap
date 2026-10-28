# Async and methods

Every decorator in python-wrap works on `async def` functions. You use it the same way; the wrapper behaves correctly for coroutines.

## What you get

When you decorate an `async def` function, python-wrap returns an `async def` wrapper. That means:

- `inspect.iscoroutinefunction(wrapped)` stays `True`, so frameworks that check it (FastAPI, Starlette, pytest-asyncio, `asyncio.to_thread` guards) treat it correctly.
- The decorator awaits your function. It does not return a coroutine to a sync wrapper that hides it.
- Waiting uses `asyncio.sleep`, so the event loop is never blocked.

```python
import asyncio
import inspect
from python_wrap import (
    circuit_breaker, fallback, memoize, once, rate_limit, retry, singleflight,
    throttle, timeout, timing,
)

@retry(max_attempts=2, delay=0.01)
@timeout(1)
@timing
async def fetch() -> str:
    return "ok"

assert inspect.iscoroutinefunction(fetch)
assert asyncio.run(fetch()) == "ok"

for decorator in (
    retry, fallback(0), memoize, once, singleflight, timeout(1), throttle(1),
    rate_limit(1, 1), circuit_breaker, timing,
):
    async def probe() -> None: ...
    assert inspect.iscoroutinefunction(decorator(probe)), decorator
```

Methods work too, including `async def` methods; `self` is just the first argument. Read [Methods](#methods) below before decorating methods with limiters or caches.

## Per-decorator behaviour

| Decorator | What is different for `async def` |
| --- | --- |
| `retry` | Awaits the function and sleeps with `asyncio.sleep` between attempts. |
| `fallback`, `suppress` | Catch exceptions from the awaited call. The `fallback` handler is still a plain (sync) function. |
| `circuit_breaker`, `CircuitBreaker` | Counts failures from the awaited call. `breaker.call(async_func, ...)` returns a coroutine to await. State changes are lock-protected but never hold the lock across an `await`. |
| `timeout` | Cancels the coroutine with `asyncio.wait_for`, which actually stops the work. |
| `memoize`, `cache` | Caches the **awaited** result. Concurrent awaits of the same missing key share one in-flight task, so the function runs once. Cancelling one caller does not cancel the shared computation. |
| `once` | Concurrent first awaits share one task. A first call that raises can be retried. |
| `singleflight` | De-duplicates across tasks on the same event loop, using a shared task. |
| `rate_limit` | With `block=True` waits with `await`; otherwise raises `RateLimitExceeded`. |
| `throttle` | Same semantics with an `async` wrapper. |
| `debounce` | Schedules a task on the running loop. Every caller in a burst awaits the same outcome (the last call's result). Tracked per event loop. |
| `run_in_thread` | The coroutine runs with `asyncio.run` in a new thread (its own loop). The wrapper is a plain function returning a `Future`. |
| `timing`, `measure_memory`, `profile` | Cover the whole awaited coroutine. (`profile` includes event-loop work for other tasks; see [Limitations](../project/limitations.md#measure_memory-and-profile).) |
| `benchmark` / `benchmark_async` | Use `benchmark_async` for coroutine functions; `benchmark` raises `TypeError`. |
| `log_args`, `log_return`, `audit`, `trace` | Log around the awaited call. |
| `validate_args`, `check_type`, `enforce_types` | Validate at call time; `enforce_types` checks the awaited result against the return annotation. |
| `revert_on_failure`, `transactional` | Work on `async def`. `Transaction` supports `async with`, and async callbacks are awaited. |
| `deprecate` | Warns on every await. |
| `mock_data` | Works on `async def`; the awaited result is the data. |

## Calling async code from sync code

Use `asyncio.run` from a plain script or thread, as the examples in these docs do:

```python
import asyncio
from python_wrap import retry

@retry(max_attempts=2, delay=0.01)
async def job() -> int:
    return 1

assert asyncio.run(job()) == 1
```

To run a coroutine function in the background from sync code, use [`run_in_thread`](../features/concurrency/run-in-thread.md). It gives the coroutine its own event loop in a new thread and hands you a `Future`.

## Methods

Decorators work on instance methods; `self` is an ordinary first argument. Two things differ from plain functions:

- **`rate_limit`, `throttle` and `debounce` keep their state on the decorated function**, so every instance shares one limiter, one throttle window and one pending call. Split the state with `key=` (for `rate_limit`) or build the decorated callable per instance in `__init__`.
- **`memoize` includes `self` in the cache key**, so results are per instance, but the cache holds a strong reference to every instance it has seen. Set `maxsize` or `ttl` to let them go.

Both are explained, with workarounds, in [Limitations](../project/limitations.md#shared-state-on-methods). To apply one decorator to every method of a class, see [`wrap_methods`](../features/devtools.md).

## Process pools and pickling

Decorated functions pickle **by reference**, like plain functions: `pickle` stores the module and qualified name, and loading it finds the same object again. You can submit a decorated module-level function to a `multiprocessing` pool or a `ProcessPoolExecutor`, including with the `"spawn"` start method (the default on macOS and Windows). This also covers objects that are not functions, such as `periodic`, and classes decorated with `wrap_methods`.

```python
import pickle
from python_wrap import count_calls, memoize, periodic, retry

@memoize
@retry(max_attempts=3, delay=0.01)
def load(key: str) -> str:
    return key.upper()

@periodic(60)
def refresh() -> None: ...

assert pickle.loads(pickle.dumps(load)) is load
assert pickle.loads(pickle.dumps(refresh)) is refresh
```

<!-- skip -->
```python
# jobs.py: workers re-import this module by name, so the function must live at module level
import concurrent.futures
import multiprocessing
from python_wrap import rate_limit, retry

@retry(max_attempts=3, delay=0.1)
@rate_limit(10, 1)
def fetch(url: str) -> int: ...

if __name__ == "__main__":
    ctx = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(4, mp_context=ctx) as pool:
        sizes = list(pool.map(fetch, ["https://a.example", "https://b.example"]))
```

**Limiter, breaker and cache state is per process.** Each worker imports the module and builds its own decorated function, so it has its own cache, rate limiter, circuit breaker, bulkhead and call counters. Above, `rate_limit(10, 1)` allows 10 calls per second *in each worker*, not across the pool. Nothing is shared or sent back to the parent.

Objects that hold that state refuse to pickle with a clear `TypeError`: `CircuitBreaker`, `RateLimiter`, `Bulkhead`, `RetryBudget`, `Transaction`, `Policy` and `testing.FakeClock`. Create them at module level so each process builds its own. Instances whose class uses `cached_property` pickle normally, but the cached values are not pickled: the copy computes them again on first access. See [Limitations](../project/limitations.md#pickling-and-process-pools).

## Gotchas

- **One event loop per `debounce` and `singleflight` burst.** Their state is tracked per loop; do not share a debounced coroutine across loops and expect them to coordinate.
- **`rate_limit(block=False)` raises inside the coroutine**, at the `await`, not when you call the function.
- **Sync decorators on an `async def` function you wrote yourself** (a `functools.wraps` wrapper that is not itself `async def`) hide the coroutine from python-wrap's detection. Stack python-wrap decorators directly on the `async def`, or make your wrapper `async def`.
