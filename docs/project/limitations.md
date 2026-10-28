# Known limitations

python-wrap tries to do the obvious thing, and where it cannot, to say so. These are the places where a decorator does less than you might assume, and what to do instead.

## `timeout`

**The thread fallback cannot stop the work.** How a sync function is interrupted depends on where it runs:

| Where | Mechanism | Stops the function? |
| --- | --- | --- |
| `async def` | `asyncio.wait_for` | Yes (the coroutine is cancelled) |
| Unix, main thread | `SIGALRM` timer | Yes (the call is interrupted) |
| Windows, non-main thread, or `use_signals=False` | Daemon worker thread; the caller stops waiting | **No** |

With the thread fallback the caller gets `TimeoutExpired` at the deadline, but the function keeps running in the background until it returns on its own, and its result is discarded. Side effects (writes, requests) still happen. If you need real cancellation, make the function cooperative (check a flag, use the library's own timeout parameter such as `requests.get(timeout=...)`), or use `async def`.

Related details:

- Signal-based timeouts need the **main thread**. `use_signals=True` raises `RuntimeError` anywhere else.
- A C call that does not release the interpreter (a long regex, a blocking C extension) is interrupted only when control returns to Python.
- `TimeoutExpired` is an ordinary exception. A `retry` with the default `on=Exception` that wraps the function inside an outer `timeout` can catch it. See [Stacking](../get-started/stacking.md#timeout-above-or-below-retry).

## Shared state on methods

`rate_limit`, `throttle` and `debounce` keep their state on the **decorated function**, not on the instance. When you decorate a method, every instance shares one limiter, one throttle window and one pending debounced call.

```python
from python_wrap import throttle

class Sensor:
    def __init__(self, name: str) -> None:
        self.name = name

    @throttle(60)
    def read(self) -> str:
        return self.name

a, b = Sensor("a"), Sensor("b")
assert a.read() == "a"
assert b.read() == "a"   # skipped: shares the window with `a`, returns its result
```

What to do:

- `rate_limit` and `singleflight` accept `key=`; use it to split the state, for example `key=lambda self, *a, **kw: id(self)`.
- For `throttle` and `debounce`, create the decorated callable per instance in `__init__` (`self.read = throttle(60)(self._read)`).

## `memoize` on methods

`self` is part of the cache key, so results are per instance, but the cache holds a **strong reference** to every instance it has seen. Instances are therefore never garbage collected while their entries are cached.

Mitigations:

- Use `per_instance=True`: each instance gets its own cache through a weak reference, so instances stay collectable (they must be weak-referenceable). See [Per-instance method caches](../features/caching/memoize.md#per-instance-method-caches).
- Set `maxsize` and/or `ttl` so entries (and the instances they pin) are eventually dropped.
- Use `key=` that ignores `self` (for results that do not depend on the instance), so the instance is not stored.
- Call `cache_clear()` when a long-lived cache has served its purpose.

Also note that for sync functions two threads that miss on the same key at the same moment may both compute the value (the function is never called while holding the lock); the last stored result wins. Put `singleflight` under `memoize` if the computation must not run twice.

## `measure_memory` and `profile`

- **`measure_memory`** uses `tracemalloc`, which is process-global. While a call runs, allocations made by other threads or asyncio tasks are counted too, and the peak counter is reset at the start of every measured call, so **concurrent measured calls share one tracker** and their numbers are not independent. Only memory allocated through Python's allocator is seen (C extensions that bypass it are invisible), and tracing slows the program.
- **`profile`** on an `async def` function enables the profiler for the whole awaited coroutine, so work the event loop does for **other tasks** while this one is suspended is included in the results. If another profiler is already active, python-wrap logs a warning once and runs the function unprofiled.

## `revert_on_failure`

It restores attribute values from a copy. In-place mutation of a container (`self.items.append(x)`) is undone, but the attribute ends up pointing at a **new** object, so other references to the old container still see the mutation. A shallow copy does not protect nested objects; use `deep=True`. Values that cannot be copied are restored by reference. It undoes attribute changes only, not external side effects, and `__slots__` classes need `attrs=`.

## `transactional`

If your function has a `tx` parameter, it is passed as the keyword `tx=`. Put `tx` **last or keyword-only** so callers cannot supply it positionally by accident. A rollback callback that raises is logged, and the remaining callbacks still run; a commit callback that raises propagates and skips the remaining commit callbacks.

## Pickling and process pools

Decorated functions pickle by reference, so the rules are those of plain functions: the function must be reachable by its module and qualified name. Functions defined inside another function, lambdas, and functions defined in `__main__` of an interactive session cannot be sent to a `"spawn"` worker.

State does not cross processes. Each worker has its own cache, rate limiter, circuit breaker, bulkhead and counters; there is no pool-wide limit. `CircuitBreaker`, `RateLimiter`, `Bulkhead`, `RetryBudget`, `Transaction`, `Policy` and `testing.FakeClock` raise `TypeError` when pickled or copied. A `cached_property` value is not pickled with its instance and is computed again after unpickling. See [Process pools and pickling](../get-started/async-and-methods.md#process-pools-and-pickling).

## Other things to know

- **`debounce`** (sync) runs the function on a daemon timer thread, so it may be abandoned at interpreter exit with a call still pending. Call `.flush()` before shutting down if the last call matters.
- **`enforce_types`** accepts, rather than rejects, annotations it cannot check (`TypedDict`, non-runtime-checkable protocols, unresolved forward references). It adds per-call cost proportional to container size. See [Validation](../features/correctness/validation.md#supported-annotations).
- **`retry`** with `max_attempts=None` retries forever. Pair it with `max_delay` and an `on=` that names only transient errors.
- **Signal timers are process-wide.** `timeout` restores the previous `SIGALRM` handler and any armed timer afterwards, but other code that installs its own `SIGALRM` handler concurrently can conflict.
