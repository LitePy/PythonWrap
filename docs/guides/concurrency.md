# Concurrency and rate limits

Control how often a function runs and how many identical calls run at once.

| Decorator | One-line behaviour |
| --- | --- |
| [`rate_limit`](#rate_limit) | At most N calls per period; reject or wait. |
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
