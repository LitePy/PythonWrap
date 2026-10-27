# Concurrency and rate limits

Control how often a function runs and how many identical calls run at once.

| Decorator | One-line behaviour |
| --- | --- |
| [`rate_limit`](#rate_limit) | At most N calls per period; reject or wait. |
| [`RateLimiter`](#one-budget-for-several-functions-ratelimiter) | One rate limit shared by several functions and blocks. |
| [`throttle`](#throttle) | At most one run per interval; skipped calls return the last result. |
| [`debounce`](#debounce) | Run once, after calls have stopped for a while. |
| [`singleflight`](#singleflight) | Identical concurrent calls share one execution. |
| [`run_in_thread`](#run_in_thread) | Start the call in a new thread and get a `Future`. |

For `timeout`, see [Resilience](resilience.md#timeout).

!!! note "State belongs to the function"
    The state kept by `rate_limit`, `throttle` and `debounce` belongs to the decorated function. On a **method** it is shared by every instance. `rate_limit` (and `singleflight`) take `key=` to split it. See [Limitations](../limitations.md#shared-state-on-methods).

## `rate_limit`

Allow at most `calls` calls per `period` seconds, using a sliding window. Unlike a fixed window, it never lets a burst of `2 * calls` through at a window boundary.

=== "Sync"

    ```python
    from python_wrap import RateLimitExceeded, rate_limit

    @rate_limit(2, 60)
    def send(text: str) -> str:
        return text

    send("one")
    send("two")
    try:
        send("three")
    except RateLimitExceeded as exc:
        assert 0 < exc.retry_after <= 60   # seconds until a slot frees
    else:
        raise AssertionError("expected the limit to apply")
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import RateLimitExceeded, rate_limit

    @rate_limit(1, 60)
    async def send(text: str) -> str:
        return text

    async def main() -> None:
        await send("one")
        try:
            await send("two")
        except RateLimitExceeded:
            pass
        else:
            raise AssertionError("expected the limit to apply")

    asyncio.run(main())
    ```

By default an over-limit call raises `RateLimitExceeded`; its `retry_after` attribute says how many seconds until a slot frees. Rejected calls are not counted.

### Waiting instead of rejecting

With `block=True` the call waits until it is allowed. Async functions wait with `await`, so the event loop is never blocked.

```python
from python_wrap import rate_limit

@rate_limit(1, 0.02, block=True)
def tick() -> int:
    return 1

assert tick() + tick() == 2   # the second call waits about 20 ms
```

### One limit per user

`key=` receives the call's arguments (including `self` for methods) and returns a hashable bucket key. Each key gets its own limit.

```python
from python_wrap import RateLimitExceeded, rate_limit

@rate_limit(1, 60, key=lambda user, *_: user)
def send(user: str, text: str) -> None: ...

send("ana", "hi")
send("ben", "hi")    # a different bucket
try:
    send("ana", "again")
except RateLimitExceeded:
    pass
else:
    raise AssertionError("ana is over her limit")
```

!!! tip "Per-instance limits on methods"
    Use `key=lambda self, *a, **kw: id(self)` (or an identifying attribute) to give each instance its own limit.

### Several limits at once

APIs often have more than one quota, such as "5 per second and 600 per 15 minutes". Pass them all with `limits=`; a call goes through only when **every** limit allows it, and a rejected call uses up none of them. `retry_after` (and the wait with `block=True`) is the time until all of them allow a call.

```python
from python_wrap import RateLimitExceeded, rate_limit

@rate_limit(limits=[(5, 1.0), (600, 900.0)])
def strava(activity: int) -> int:
    return activity

for n in range(5):
    strava(n)
try:
    strava(5)                        # the per-second limit is used up
except RateLimitExceeded as exc:
    assert 0 < exc.retry_after <= 1
else:
    raise AssertionError("expected the limit to apply")
```

`limits=` can also be combined with the positional pair: `rate_limit(5, 1.0, limits=[(600, 900.0)])`.

### One budget for several functions: `RateLimiter`

`rate_limit` gives each decorated function its own budget. When several functions call the same service, create one `RateLimiter` and use it everywhere: as a decorator, as `with limiter:` / `async with limiter:` around a block, or through its methods.

```python
from python_wrap import RateLimitExceeded
from python_wrap.concurrency import RateLimiter

github = RateLimiter(2, 60, name="github")

@github
def get_repo(name: str) -> str:
    return name

@github
def get_user(name: str) -> str:
    return name

get_repo("python-wrap")
get_user("ana")            # same budget: 2 of 2 used
try:
    with github:           # a plain block counts against it too
        pass
except RateLimitExceeded as exc:
    assert "github rate limit" in str(exc)
else:
    raise AssertionError("expected the shared limit to apply")
```

`RateLimiter(calls, period, *, limits=None, block=False, name=None)` takes the same limits as `rate_limit`. `block` decides what the decorator and `with` do when over the limit (raise, or wait). For explicit control:

| Method | Behaviour |
| --- | --- |
| `try_acquire()` | Take a slot if one is free now; returns `True` or `False`. |
| `acquire(block=True, timeout=None)` | Wait in this thread for a slot. With `timeout`, returns `False` as soon as the next slot is further away than that. |
| `await acquire_async(block=True, timeout=None)` | The same for async code; never blocks the event loop. |

```python
import asyncio
from python_wrap.concurrency import RateLimiter

limiter = RateLimiter(1, 0.02, block=True)

async def main() -> None:
    async with limiter:
        pass
    async with limiter:            # waits about 20 ms, without blocking the loop
        pass
    assert not limiter.try_acquire()
    assert await limiter.acquire_async(timeout=1)

asyncio.run(main())
```

### Rate limits in tests and metrics

`python_wrap.testing.disable_rate_limits()` makes `rate_limit`, `RateLimiter` and `throttle` let every call through. The switch is read at call time, so it also works on functions decorated at import. Under `fast_mode()` limiters keep counting: a non-blocking call over the limit is still rejected, and a `block=True` call that would wait goes through at once (and is recorded). See [Testing](../testing.md).

To keep limits on in a test but start each test with a fresh budget, call `RateLimiter.reset()`: it forgets every recorded call, for every key. Use a shared `RateLimiter` you can reach from the test, or get the limiter of a `rate_limit`-decorated function with `limiter_of(func)` (it returns `None` for a function without one; `throttle` has no reset):

```python
from python_wrap.concurrency import limiter_of, rate_limit

@rate_limit(1, 60)
def send() -> str:
    return "sent"

assert send() == "sent"
limiter = limiter_of(send)
assert limiter is not None
limiter.reset()                    # e.g. in a fixture, between tests
assert send() == "sent"
```

Rate limits emit `rate_limit.rejected` (`retry_after`) when a call is refused and `rate_limit.waited` (`seconds`) after a call had to wait; `throttle` emits `throttle.skipped` for each skipped call. See [Observability](observability.md).

## `throttle`

Run at most once per `interval` seconds. The first call runs and opens a window; calls inside the window do not run and return **the result of the last call that did run** (`None` if none has finished yet, or it raised).

=== "Sync"

    ```python
    from python_wrap import throttle

    runs = []

    @throttle(60)
    def refresh() -> int:
        runs.append(1)
        return len(runs)

    assert refresh() == 1
    assert refresh() == 1      # skipped: returns the previous result
    assert len(runs) == 1
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import throttle

    runs = []

    @throttle(60)
    async def refresh() -> int:
        runs.append(1)
        return len(runs)

    async def main() -> None:
        assert await refresh() == 1
        assert await refresh() == 1
        assert len(runs) == 1

    asyncio.run(main())
    ```

A call that raises still uses up its window. The wrapped function returns `R | None` for the type checker.

Use `throttle` for "update the progress bar", "sync at most once a minute" and similar. Use `rate_limit` when excess calls must be rejected or delayed rather than silently skipped.

## `debounce`

Postpone running until `wait` seconds have passed since the **last** call. Each call restarts the countdown and replaces the pending arguments, so only the final call of a burst runs. This is the right tool for "save after the user stops typing".

=== "Sync"

    ```python
    import time
    from python_wrap import debounce

    saved = []

    @debounce(0.02)
    def save(text: str) -> None:
        saved.append(text)

    save("a")
    save("ab")
    save("abc")            # each call returns None immediately
    time.sleep(0.04)       # the timer thread runs the last call
    assert saved == ["abc"]
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import debounce

    saved = []

    @debounce(0.02)
    async def save(text: str) -> str:
        saved.append(text)
        return text

    async def main() -> None:
        # Every caller in a burst awaits the same outcome: the final call's result.
        results = await asyncio.gather(save("a"), save("ab"), save("abc"))
        assert results == ["abc", "abc", "abc"]
        assert saved == ["abc"]

    asyncio.run(main())
    ```

The wrapper also has:

- `.cancel()` drops the pending call (async awaiters get `CancelledError`).
- `.flush()` runs the pending call right now and returns its result, or `None` if nothing is pending. For `async def` functions it is a coroutine to `await`.

```python
from python_wrap import debounce

saved = []

@debounce(60)
def save(text: str) -> None:
    saved.append(text)

save("draft")
save("final")
save.flush()                 # run now instead of waiting a minute
assert saved == ["final"]

save("never saved")
save.cancel()                # drop it
assert saved == ["final"]
```

Sync debounced functions run on a daemon timer thread, so the function body runs on a different thread from the caller. Calls must be awaited (or wrapped in tasks) for async debounce to take effect.

## `singleflight`

While a call for a given key is running, further calls with the same key wait and receive **the same result or exception**. Once it finishes, the next call runs fresh. This de-duplicates work in flight; it is not a cache. Use it to stop a thundering herd of identical requests (a cache miss that every request tries to fill).

=== "Sync"

    ```python
    import time
    from concurrent.futures import ThreadPoolExecutor
    from python_wrap import singleflight

    calls = []

    @singleflight
    def load(path: str) -> str:
        calls.append(path)
        time.sleep(0.03)
        return f"contents of {path}"

    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(load, ["a.txt"] * 3))

    assert results == ["contents of a.txt"] * 3
    assert len(calls) == 1
    ```

=== "Async"

    ```python
    import asyncio
    from python_wrap import singleflight

    calls = []

    @singleflight
    async def load(path: str) -> str:
        calls.append(path)
        await asyncio.sleep(0.01)
        return f"contents of {path}"

    async def main() -> None:
        results = await asyncio.gather(*(load("a.txt") for _ in range(3)))
        assert results == ["contents of a.txt"] * 3
        assert calls == ["a.txt"]

    asyncio.run(main())
    ```

Sync functions de-duplicate across threads. Async functions de-duplicate across tasks on the same event loop; the work runs in a shared task, so cancelling one waiting caller does not cancel it for the others.

The default key is the positional arguments plus sorted keyword arguments (including `self` for methods), which must be hashable. Pass `key=` to choose what counts as "the same call", for example `key=lambda path, **_: path`.

## `run_in_thread`

Start each call in a new thread and get a `concurrent.futures.Future` back.

```python
from python_wrap import run_in_thread

@run_in_thread
def add(a: int, b: int) -> int:
    return a + b

future = add(1, 2)
assert future.result() == 3    # waits; re-raises the function's exception, if any
```

- `future.result()` is the equivalent of v0.1's `thread.join()`, plus the return value.
- `async def` functions run with `asyncio.run` in the new thread (their own event loop), and the wrapper itself is a plain function returning a `Future`.
- The caller's `contextvars` are copied into the thread.
- Options: `daemon=True` (do not keep the interpreter alive) and `name=` (thread name).

```python
import asyncio
from python_wrap import run_in_thread

@run_in_thread(daemon=True, name="worker")
async def fetch() -> str:
    await asyncio.sleep(0.01)
    return "done"

assert fetch().result() == "done"
```
