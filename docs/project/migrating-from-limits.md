# Migrating from limits

The `limits` library separates a *rate limit item* (`5/minute`), a *storage* (memory, Redis, ...) and a *strategy* (fixed window, moving window). python-wrap folds these into a decorator or a small `RateLimiter` object: you give it `calls` and `period`, and it uses a sliding-window log. This page shows how each `limits` idea maps over and where the behaviour differs. There is no codemod for it, because `limits` code is call-site shaped rather than decorator shaped.

## At a glance

=== "limits"

    <!-- skip -->
    ```python
    from limits import parse, storage, strategies

    limiter = strategies.MovingWindowRateLimiter(storage.MemoryStorage())
    item = parse("5/minute")

    def send(user_id: str) -> None:
        if not limiter.hit(item, "send", user_id):
            raise RuntimeError("slow down")
        ...
    ```

=== "python-wrap"

    ```python
    from python_wrap import RateLimitExceeded, rate_limit

    @rate_limit(5, 60, key=lambda user_id: user_id)
    def send(user_id: str) -> str:
        return f"sent for {user_id}"

    assert send("u1") == "sent for u1"
    try:
        for _ in range(10):
            send("u1")
    except RateLimitExceeded as exc:
        print(f"retry in {exc.retry_after:.0f}s")
    ```

## Rate items and identifiers

| limits | python-wrap |
| --- | --- |
| `parse("5/minute")`, `RateLimitItemPerMinute(5)` | `rate_limit(5, 60)` or `RateLimiter(5, 60)` (periods are seconds) |
| `RateLimitItemPerSecond(10)` | `rate_limit(10, 1)` |
| `RateLimitItemPerHour(100)` | `rate_limit(100, 3600)` |
| `RateLimitItemPerMinute(5, multiples=2)` ("5 per 2 minutes") | `rate_limit(5, 120)` |
| several limits on one resource | `rate_limit(limits=[(5, 1.0), (600, 900.0)])`; every limit must allow the call |
| identifiers: `limiter.hit(item, "user", user_id)` | `rate_limit(..., key=lambda user_id: user_id)`; each key has its own window |
| namespace shared by several functions | one `RateLimiter` used as the decorator on each of them |

## Strategies

| limits | python-wrap |
| --- | --- |
| `MovingWindowRateLimiter` | the only mode: `rate_limit` / `RateLimiter` are exact sliding windows |
| `FixedWindowRateLimiter` | no fixed-window mode. Use the sliding window; it never lets a `2 * calls` burst through at a boundary, so it is stricter, not looser |
| `FixedWindowElasticExpiryRateLimiter` | no equivalent |
| `SlidingWindowCounterRateLimiter` | use the sliding window (exact rather than approximated) |

If you need counts that reset on a clock boundary (a daily quota that resets at midnight), keep the counter outside python-wrap or use a `Quota` per day (below).

## Calls

| limits | python-wrap |
| --- | --- |
| `limiter.hit(item, *ids)` -> `bool` | `limiter.try_acquire()` -> `bool` (consumes a slot when it returns `True`) |
| `if not limiter.hit(...): raise` | `@rate_limit(...)`; over the limit raises `RateLimitExceeded` |
| `limiter.test(item, *ids)` (peek) | none; `try_acquire()` always consumes. Use the exception from the decorator |
| `limiter.get_window_stats(item, *ids)` (`reset_time`, `remaining`) | `RateLimitExceeded.retry_after` says when a slot frees; no remaining-count API |
| `limiter.clear(item, *ids)` | `limiter_of(func).reset()` or `RateLimiter.reset()` |
| waiting for capacity (hand-rolled sleep loop) | `rate_limit(..., block=True)`, or `limiter.acquire(timeout=...)`; async code awaits instead of blocking |

```python
from python_wrap import RateLimiter

limiter = RateLimiter(limits=[(3, 1.0), (100, 3600.0)], name="api")

@limiter
def get_user(user_id: str) -> str:
    return user_id

@limiter
def get_repo(name: str) -> str:
    return name

assert get_user("a") == "a" and get_repo("b") == "b"   # one shared budget
assert limiter.try_acquire()
assert not limiter.try_acquire()   # 3 calls in the last second
```

```python
import asyncio
from python_wrap import rate_limit

@rate_limit(2, 0.05, block=True)
async def fetch(n: int) -> int:
    return n

async def main() -> list[int]:
    return [await fetch(i) for i in range(4)]   # waits instead of raising

assert asyncio.run(main()) == [0, 1, 2, 3]
```

In tests, `python_wrap.testing` can disable rate limits or use `fast_mode()`/`fake_clock()` so a blocking call returns at once.

## Storages

| limits | python-wrap |
| --- | --- |
| `MemoryStorage()` / `memory://` | the default; in-process, thread-safe, fork-safe |
| `RedisStorage("redis://...")` / `redis://` | `RedisWindow(client, key, calls=, period=)` |
| `RedisClusterStorage`, `RedisSentinelStorage` | `RedisWindow` with a cluster-aware client you create; each decision touches a single key |
| `MemcachedStorage`, `MongoDBStorage`, `EtcdStorage` | not supported |
| `storage.check()` health probe | your Redis client's own `ping()` |

A Redis window is built from a client you create (python-wrap never imports `redis` itself). Its admission decision is one atomic Lua script using the **Redis server's clock**, so workers with skewed clocks agree.

=== "limits"

    <!-- skip -->
    ```python
    from limits import parse
    from limits.storage import RedisStorage
    from limits.strategies import MovingWindowRateLimiter

    limiter = MovingWindowRateLimiter(RedisStorage("redis://localhost:6379"))
    item = parse("30/minute")

    def fetch_trials() -> None:
        if not limiter.hit(item, "vendor-api"):
            raise RuntimeError("rate limited")
        ...
    ```

=== "python-wrap"

    <!-- skip -->
    ```python
    import redis
    from python_wrap import RateLimiter, RedisWindow, shared_rate_limit

    client = redis.Redis()
    window = RedisWindow(client, key="myapp:vendor-api", calls=30, period=60)

    # as a decorator that waits for a slot
    @shared_rate_limit(window, block=True)
    def fetch_trials() -> None: ...

    # or as a reusable limiter object (context manager, try_acquire, ...)
    limiter = RateLimiter(backend=window, name="vendor-api", block=True)
    ```

`shared_rate_limit(window)` raises `RateLimitExceeded` (with `retry_after`) when not blocking, exactly like the in-process decorator. Backend errors propagate, so a Redis outage is visible rather than silently allowing every call.

## Quotas are not rate limits

`limits` rate items are always per time window. When you want "at most N units for this whole job" (an import budget, a number of paid API calls per run), use `Quota`. It never refills by itself; `reset()` is an explicit act.

```python
from python_wrap import Quota, QuotaExhausted

budget = Quota(2, name="paid-calls")
assert budget.take() == 1
assert budget.take() == 0
try:
    budget.take()
except QuotaExhausted:
    print("budget spent")
assert budget.remaining == 0
```

To share a quota across workers pass `client=` and a unique `key=` for the run, with an optional `ttl=` so old keys expire.

## Behavioural differences to check

1. **No fixed window.** If your tests assert "the 6th call in the same minute fails, the 6th after the minute boundary succeeds", expect sliding-window behaviour: slots free up one by one as the oldest call ages out.
2. **Rejected calls are free.** A call that was rejected, or is waiting, is not counted against the limit.
3. **Return value.** `hit()` returns a boolean; the decorator raises. Use `limiter.try_acquire()` to keep an if-style flow.
4. **Per-key windows are in memory.** `key=` creates a window per distinct key and they live in the process, so keep the key space to values you actually serve (user ids, not raw request paths). A `RedisWindow` keeps one Redis key per identifier for shared state.
5. **Sync and async.** The same decorator handles both; async callers wait with `await` and never block the event loop.
6. **Errors.** `RateLimitExceeded` is a `PythonWrapError` and carries `retry_after`. Map it to your framework's `429 Too Many Requests` and set `Retry-After` from it.
7. **Observability.** `rate_limit.rejected` and `rate_limit.waited` events are emitted for each rejection or wait, under the `name=` of the limiter.
