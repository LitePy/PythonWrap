# Adaptive limits and per-key pools

A fixed [bulkhead](bulkheads.md) needs a number chosen in advance. `AdaptiveLimiter` finds it for you: it starts at `initial`, grows by one after a window of fast successes, and shrinks by a ratio when calls time out, drop connections or get slow. Calls beyond the current limit are shed immediately with `LoadShed` instead of queueing.

```python
from python_wrap.adaptive import AdaptiveLimiter, LoadShed

limiter = AdaptiveLimiter(initial=2, min_limit=1, max_limit=50, name="search")


@limiter
def search(query: str) -> str:
    return query.upper()


assert search("a") == "A"

first, second = limiter.acquire(), limiter.acquire()
try:
    limiter.acquire()  # two calls in flight, limit is 2
except LoadShed as shed:
    assert shed.limit == 2 and shed.in_flight == 2
first.release()
second.release()
assert limiter.in_flight == 0
```

## When to use it

- Load shedding for a multi-tenant service: protect a database, a search backend or a downstream API whose capacity you cannot size by hand and that changes over time.
- You would rather answer `503` quickly than let latency climb for everyone.
- For a fixed, known capacity with a bounded queue, use a [`Bulkhead`](bulkheads.md). To limit calls per time window, use [rate limits](rate-limits.md).

## How the limit moves

| Event | Effect |
| --- | --- |
| A call succeeds (and is under `target_latency`, if set) | After `window` such samples (default: the current limit) the limit grows by `increase`, up to `max_limit`. |
| A call raises one of `on` (default `TimeoutError`, `ConnectionError`, `BulkheadFull`) | The limit is multiplied by `backoff_ratio` (default 0.9), down to `min_limit`, and the sample count restarts. |
| A call finishes slower than `target_latency` | Counts as an overload signal, same as an error. |
| A call raises any other exception | The slot is released and the limit is left alone. |

```python
from python_wrap.adaptive import AdaptiveLimiter

limiter = AdaptiveLimiter(initial=20, backoff_ratio=0.5)
limiter.acquire().release(error=TimeoutError())
assert limiter.limit == 10

small = AdaptiveLimiter(initial=2, window=2, max_limit=10)
for _ in range(2):
    small.acquire().release()
assert small.limit == 3
```

## Latency target

With `target_latency`, slowness alone backs the limit off, which catches a dependency that degrades without failing. Time is read through the package clock, so `fake_clock()` controls it in tests.

```python
from python_wrap.adaptive import AdaptiveLimiter
from python_wrap.testing import fake_clock

limiter = AdaptiveLimiter(initial=10, target_latency=0.5, backoff_ratio=0.5)
with fake_clock() as clock:
    permit = limiter.acquire()
    clock.advance(0.9)  # the call took 0.9 s
    permit.release()
assert limiter.limit == 5
```

## Priorities

`limiter.acquire(priority=...)` takes `"low"`, `"normal"` (default) or `"critical"`. Low-priority calls (reports, prefetching) are shed once `in_flight >= limit * low_priority_share` (default 0.8), keeping headroom for the rest. Normal and critical calls are shed only at the full limit; the label shows up in `LoadShed.priority` so you can tell what was dropped.

```python
from python_wrap.adaptive import AdaptiveLimiter, LoadShed

limiter = AdaptiveLimiter(initial=10, low_priority_share=0.5)
held = [limiter.acquire() for _ in range(5)]
try:
    limiter.acquire(priority="low")
except LoadShed as shed:
    assert shed.priority == "low"
held.append(limiter.acquire(priority="critical"))  # still has room


@limiter.guard(priority="low")
def prefetch() -> None: ...
```

## Context manager, async, stats

`acquire()` returns a `Permit` that works in `with` and `async with`; leaving with an error that matches `on` backs the limit off. The decorator works on `async def` functions too. `limiter.limit`, `limiter.in_flight` and `limiter.stats` (`admitted`, `shed`, `successes`, `drops`, `increases`, `decreases`) expose its state.

```python
import asyncio
from python_wrap.adaptive import AdaptiveLimiter, LoadShed

limiter = AdaptiveLimiter(initial=1, max_limit=1)


@limiter
async def query(n: int) -> int:
    await asyncio.sleep(0.01)
    return n


async def main() -> None:
    results = await asyncio.gather(query(1), query(2), return_exceptions=True)
    assert results[0] == 1 and isinstance(results[1], LoadShed)
    async with limiter.acquire():
        pass
    assert limiter.stats.shed == 1


asyncio.run(main())
```

`LoadShed` subclasses `BulkheadFull`, so existing handlers for bulkhead rejections (and the `bulkhead`-aware parts of your error mapping, such as returning 503) cover it.

## Events

| Event | Fields | When |
| --- | --- | --- |
| `adaptive.limit_changed` | `old`, `new` | The whole-number limit changed. |
| `adaptive.shed` | `limit`, `in_flight` | A call was rejected. |

## One limiter per tenant

`PerKeyLimiter(factory, max_keys=10_000)` creates one limiter per key on first use, keeps keys in least-recently-used order, and evicts old *idle* keys when the pool is full (a key with calls in flight is never evicted). `per_key(factory, key=...)` applies it to a function so each tenant gets its own limit.

```python
from python_wrap.adaptive import AdaptiveLimiter, LoadShed, PerKeyLimiter, per_key
from python_wrap.bulkhead import Bulkhead

pool = PerKeyLimiter(lambda tenant: AdaptiveLimiter(initial=2), max_keys=1000)
assert pool.get("acme") is pool.get("acme")
assert pool.get("globex") is not pool.get("acme")


@per_key(lambda tenant: Bulkhead(5, max_waiting=0), key=lambda tenant, **_: tenant)
def handle(tenant: str) -> str:
    return f"handled {tenant}"


assert handle("acme") == "handled acme"
assert "acme" in handle.limiters
```

`rate_limit(..., key=...)` and `RateLimiter` already split their window per key, with the same `key` callable; use those for call-rate limits:

```python
from python_wrap import rate_limit


@rate_limit(100, 60, key=lambda tenant, *a, **kw: tenant)
def export(tenant: str) -> str:
    return tenant


assert export("acme") == "acme"
```

Use `per_key` for bulkheads and adaptive limiters, or when each key needs a different configuration (`factory` receives the key). It also accepts `lambda key: RateLimiter(100, 60)`. Evicting a rate limiter forgets its recent call history, so size `max_keys` above your active tenant count.
