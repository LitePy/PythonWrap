# Rate limits

Allow at most N calls per time window, and either reject or delay the rest. This keeps you inside an API quota.

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

## When to use it

- A third-party API allows N requests per second, minute or hour.
- You want to protect your own service from one caller that sends too much (use `key=`).
- Several functions call the same service and must share one budget ([`RateLimiter`](#one-budget-for-several-functions-ratelimiter)).
- Not for skipping excess calls silently: see [`throttle`](throttle-debounce.md#throttle). Not for capping calls in flight: see [Bulkheads](bulkheads.md).

## How it counts

`rate_limit(calls, period)` allows at most `calls` calls per `period` seconds, using a sliding window. Unlike a fixed window, it never lets a burst of `2 * calls` through at a window boundary.

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
        assert 0 < exc.retry_after <= 60
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

## Waiting instead of rejecting

With `block=True` the call waits until it is allowed. Async functions wait with `await`, so the event loop is never blocked.

```python
from python_wrap import rate_limit

@rate_limit(1, 0.02, block=True)
def tick() -> int:
    return 1

assert tick() + tick() == 2   # the second call waits about 20 ms
```

## One limit per user

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

## Several limits at once

APIs often have more than one quota, such as "5 per second and 600 per 15 minutes". Pass them all with `limits=`. A call goes through only when **every** limit allows it, and a rejected call uses up none of them. `retry_after` (and the wait with `block=True`) is the time until all of them allow a call.

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

## One budget for several functions: RateLimiter

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
| `reset()` | Forget every recorded call, for every key. |

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

## Events

| Event | Fields | Emitted when |
| --- | --- | --- |
| `rate_limit.rejected` | `retry_after` | A call is refused. |
| `rate_limit.waited` | `seconds` | A `block=True` call had to wait. |

See [Metrics](../observability/metrics.md).

## Testing

- `python_wrap.testing.disable_rate_limits()` makes `rate_limit`, `RateLimiter` and `throttle` let every call through. The switch is read at call time, so it also works on functions decorated at import.
- Under `fast_mode()` limiters keep counting: a non-blocking call over the limit is still rejected, and a `block=True` call that would wait **goes through at once** instead of sleeping (and is recorded).
- To keep limits on but start each test with a fresh budget, call `RateLimiter.reset()`. Use a shared `RateLimiter` you can reach from the test, or get the limiter of a `rate_limit`-decorated function with `limiter_of(func)`. It returns `None` for a function without one; `throttle` has no reset.

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

See [Testing](../../testing/index.md).

## Gotchas

!!! warning "State belongs to the function"
    The limit belongs to the decorated function. On a **method** it is shared by every instance. Use `key=` to split it, as shown above. See [Limitations](../../project/limitations.md#shared-state-on-methods).

## See also

- [Throttle and debounce](throttle-debounce.md), [Bulkheads](bulkheads.md)
- [API reference](../../api/concurrency.md)
