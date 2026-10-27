# Migrating from 0.1 to 0.2

Version 0.2 is a rewrite of the internals with a handful of deliberate breaking changes. Most v0.1 code keeps working: `from python_wrap.decorators import x` still resolves (a compatibility module re-exports everything), and the new exceptions subclass the closest built-ins.

**Checklist**

- [ ] Python 3.10 or newer ([Python 3.8 and 3.9 dropped](#python-38-and-39-are-dropped)).
- [ ] `run_in_thread` callers: `.join()` becomes `.result()`.
- [ ] `benchmark` is now a function, not a decorator.
- [ ] Replace `retry_on_exception` with `retry(on=...)`.
- [ ] Anything catching a bare `Exception` from `rate_limit`: catch `RateLimitExceeded`.
- [ ] Logging: configure it yourself, and expect `DEBUG` as the default level for `log_args`, `log_return` and `trace`.
- [ ] Remove `no_debug`.
- [ ] `validate_args` / `check_type` with a misspelled parameter name now fail at import time.

Run your test suite with `-W error::DeprecationWarning` to find deprecated calls.

## Breaking changes

### `run_in_thread` returns a `Future`

It used to return a `threading.Thread`, with no way to get the function's result. It now returns a `concurrent.futures.Future`.

=== "0.1"

    <!-- skip -->
    ```python
    @run_in_thread
    def work():
        ...

    thread = work()
    thread.join()
    ```

=== "0.2"

    ```python
    from python_wrap import run_in_thread

    @run_in_thread
    def work() -> int:
        return 42

    future = work()
    assert future.result() == 42   # was thread.join(); also returns the value
    ```

`future.result()` waits like `join()`, returns the value, and re-raises the function's exception. Use `future.done()` where you used `thread.is_alive()` (inverted).

### `benchmark` is a function {#benchmark-is-a-function}

The v0.1 decorator silently ran your function three times on every call. In 0.2 you call `benchmark` explicitly and get a `BenchmarkResult` back. Use `benchmark_async` for `async def` functions.

=== "0.1"

    <!-- skip -->
    ```python
    @benchmark
    def work():
        ...

    work()   # ran three times, printed timings
    ```

=== "0.2"

    ```python
    from python_wrap import benchmark

    def work(n: int) -> int:
        return sum(range(n))

    result = benchmark(work, args=(1000,), runs=5, warmup=1)
    assert result.runs == 5
    print(result)
    ```

### `retry_on_exception` is deprecated and bounded

`retry_on_exception` still works but emits a `DeprecationWarning`, and its default changed: it used to retry **forever with no delay**; it now stops after 3 attempts. Switch to `retry(on=...)`.

=== "0.1"

    <!-- skip -->
    ```python
    @retry_on_exception(ConnectionError)
    def fetch():
        ...
    ```

=== "0.2"

    ```python
    from python_wrap import retry

    @retry(on=ConnectionError, max_attempts=3, delay=0.5, backoff=2)
    def fetch() -> None:
        ...
    ```

If you relied on unlimited retries, say so explicitly with `max_attempts=None`, and add a `delay` so you do not spin:

```python
from python_wrap import retry

@retry(on=ConnectionError, max_attempts=None, delay=1.0, max_delay=30, backoff=2)
def wait_for_service() -> None:
    ...
```

Note that `retry` defaults to `delay=1.0` while the deprecated `retry_on_exception` defaults to `delay=0.0`.

### `rate_limit` raises `RateLimitExceeded`

It used to raise a bare `Exception`. `RateLimitExceeded` is a `PythonWrapError` (so it is still an `Exception`) and carries `retry_after`.

=== "0.1"

    <!-- skip -->
    ```python
    try:
        send()
    except Exception:
        ...   # could be anything
    ```

=== "0.2"

    ```python
    from python_wrap import RateLimitExceeded, rate_limit

    @rate_limit(1, 60)
    def send() -> None: ...

    send()
    try:
        send()
    except RateLimitExceeded as exc:
        wait = exc.retry_after
    ```

New in 0.2: `rate_limit(block=True)` waits instead of raising, and `key=` gives each user their own limit.

### Logging defaults changed

Two changes you may notice as "my logs disappeared":

1. **Importing python-wrap no longer calls `logging.basicConfig`.** v0.1 configured the root logger as a side effect. 0.2 never configures logging; the `python_wrap` logger has a `NullHandler`. Configure logging in your application.
2. **`log_args`, `log_return` and `trace` log at `DEBUG`** (`log_args` used to use `INFO`). Set the level, or pass `level=logging.INFO` to restore the old output.

=== "0.1"

    <!-- skip -->
    ```python
    # Logged to the root logger at INFO with no setup of your own.
    @log_args
    def work(x): ...
    ```

=== "0.2"

    ```python
    import logging
    from python_wrap import log_args

    logging.basicConfig(level=logging.DEBUG)       # you configure logging

    @log_args(level=logging.INFO)                  # or keep the old level
    def work(x: int) -> None: ...

    work(1)
    ```

Every logging decorator also accepts `logger=` (a logger or a name) and `level=`. See [Observability](guides/observability.md#configuring-logging).

### `no_debug` is deprecated

`no_debug` is now a no-op that warns and will be removed in 0.3. Delete it, and use logging levels to hide debug output.

### Python 3.8 and 3.9 are dropped

python-wrap 0.2 requires Python 3.10 or newer. Stay on 0.1 if you cannot upgrade. The two runtime dependencies v0.1 declared but did not use (`wrapt`, `typing-extensions`) are gone: 0.2 has no dependencies.

### Unknown parameter names fail at decoration time

`validate_args` and `check_type` now raise `TypeError` when you decorate if a keyword is not a parameter of the function, so a typo is found at import rather than silently never validating.

=== "0.1"

    <!-- skip -->
    ```python
    @validate_args(amout=lambda a: a > 0)   # typo: silently validated nothing
    def charge(amount): ...
    ```

=== "0.2"

    ```python
    from python_wrap import validate_args

    try:
        @validate_args(amout=lambda a: a > 0)
        def charge(amount: int) -> None: ...
    except TypeError as exc:
        print(exc)   # names the unknown parameter
    ```

## Behaviour details to know

These are not breaking for most code, but they can surprise you if you read the early 0.2 notes or wrote your own wrappers around python-wrap.

- **`timeout(...)` returns a `Timeout` object.** It is still a decorator (`@timeout(5)` and `@timeout(5)` on `async def` are unchanged), and is now also a context manager: `with timeout(5):` and `async with timeout(5):`.
- **`retry` never retries cancellation-type exceptions.** `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` propagate immediately, even if you list them (or `BaseException`) in `on=`. Shutdown and task cancellation are no longer swallowed by a retry loop.
- **`redact=` on `log_args` and `audit` defaults to `None`.** `None` means "use the value from `configure()`, or `DEFAULT_REDACT`". Passing `redact=()` still turns redaction off, and `redact_extra=` adds fragments to whichever list is in force.
- **`audit` may skip building a record.** With no `sink=` and the log level disabled, nobody could see the `AuditRecord`, so none is built (no work, no `include_result` evaluation). Pass a `sink=` if you always need the record.

## Behaviour that was broken and is now fixed

These were bugs in v0.1. Code that worked around them may need to change.

| Area | v0.1 | 0.2 |
| --- | --- | --- |
| `revert_on_failure` | Claimed to revert, but its snapshot was always empty. | Actually restores `vars(obj)`, or `attrs=`; `deep=` and `target=` available. |
| `transactional` | A no-op. | A real `Transaction` with `on_commit` / `on_rollback`, `tx=` injection and `current_transaction()`. |
| `memoize` / `cache` keys | Built from `str(args)`, so `f(1)` and `f(x=1)` were different entries. | Signature-bound, defaults applied, unhashable lists/dicts/sets handled. `cache()` no longer shares one store across functions. |
| `timing` on `async def` | Measured about 0 seconds. | Times the whole awaited coroutine; uses `perf_counter`. |
| `measure_memory` | `ru_maxrss`: Unix-only, wrong units. | `tracemalloc`; works everywhere. |
| `timeout` | Thread fallback undocumented. | `SIGALRM` interruption on Unix main thread, thread fallback documented, `asyncio.wait_for` for async; raises `TimeoutExpired` (a `TimeoutError`). |
| `deprecate` | The warning pointed into the library. | Points at the caller; sets `__deprecated__` (PEP 702); works on classes. |
| `audit` | Reported the wrapper's frame as the caller. | Reports the real caller; returns an `AuditRecord`. |
| `once` | Not thread-safe. | Thread-safe; retries after an exception; `.reset()` and `.called`. |
| `validate_args` | Rebuilt the signature on every call. | Binds once at decoration. |
| Windows | Import failed (imported Unix-only `resource`). | Works. |

If you depended on `revert_on_failure` or `transactional` doing nothing, check those call sites: they now do what they say.

## New in 0.2

You do not need these to migrate, but they replace common hand-written code:

- `retry`: `backoff`, `max_delay`, `jitter`, `on=`, `retry_if=`, `on_retry=`, unlimited attempts.
- `fallback`, `suppress`, `CircuitBreaker` / `circuit_breaker`.
- `memoize(maxsize=, ttl=, typed=, key=)` with `cache_info`, `cache_clear`, `cache_delete`.
- `throttle`, `debounce`, `singleflight`.
- `enforce_types` for runtime checks from annotations.
- `compose`, `mock_data(enabled=...)`, `deprecate(since=, removed_in=, alternative=)`.
- `retry(delay_from=retry_after_header, max_time=)`, `retrying(...)` blocks, `deadline()` and `with timeout(...)`.
- `Bulkhead`, `RateLimiter`, `gather_limit` / `map_async` / `race`, `batch_calls` and `hedge`.
- `cached_property(ttl=)`, `count_calls`, the `python_wrap.events` hook and `log_context`.
- `policy(...)` and `configure(...)` for reusable stacks and project-wide defaults.
- `python_wrap.testing`: `fast_mode`, `FakeClock`, `spy` and pytest fixtures.

See the [changelog](changelog.md) for the full list.
