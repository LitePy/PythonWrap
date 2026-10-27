# Async support

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

Methods work too, including `async def` methods; `self` is just the first argument.

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
| `timing`, `measure_memory`, `profile` | Cover the whole awaited coroutine. (`profile` includes event-loop work for other tasks; see [Limitations](limitations.md#measure_memory-and-profile).) |
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

To run a coroutine function in the background from sync code, use [`run_in_thread`](guides/concurrency.md#run_in_thread). It gives the coroutine its own event loop in a new thread and hands you a `Future`.

## Gotchas

- **One event loop per `debounce` and `singleflight` burst.** Their state is tracked per loop; do not share a debounced coroutine across loops and expect them to coordinate.
- **`rate_limit(block=False)` raises inside the coroutine**, at the `await`, not when you call the function.
- **Sync decorators on an `async def` function you wrote yourself** (a `functools.wraps` wrapper that is not itself `async def`) hide the coroutine from python-wrap's detection. Stack python-wrap decorators directly on the `async def`, or make your wrapper `async def`.
