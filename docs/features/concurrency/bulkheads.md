# Bulkheads

Cap how many calls use a limited resource at once, and refuse to build an unbounded queue in front of it.

```python
import asyncio
from python_wrap.bulkhead import Bulkhead
from python_wrap.errors import BulkheadFull

payments = Bulkhead(2, max_waiting=1, name="payments")

@payments
async def charge(amount: int) -> int:
    await asyncio.sleep(0.01)
    return amount

async def main() -> None:
    results = await asyncio.gather(*(charge(n) for n in range(4)), return_exceptions=True)
    assert results[:3] == [0, 1, 2]           # two ran at once, one waited
    assert isinstance(results[3], BulkheadFull)  # the queue was full

asyncio.run(main())
```

## When to use it

- A dependency has limited capacity: a connection pool, a rate-limited API, a CPU-heavy section.
- You prefer failing fast (`BulkheadFull`) to piling up waiting callers.
- For "at most N of these at once" with no queue limit, use [`limit_concurrency`](#limit_concurrency).
- To limit calls per time window instead, use [Rate limits](rate-limits.md). To bound a batch of awaitables you are starting yourself, use [`gather_limit` or `map_async`](async-helpers.md).

## Bulkhead

| Argument | Meaning |
| --- | --- |
| `max_concurrent` | Calls allowed to run at once. |
| `max_waiting` | Callers allowed to queue for a slot. `None` = unlimited, `0` = never wait. |
| `timeout` | Seconds a queued caller waits before giving up with `BulkheadFull`. |
| `name` | Shown in error messages and `repr`. |

One bulkhead can guard several functions and blocks at once. Use it as a decorator (sync or `async def`), as `with bulkhead:` in threaded code, or as `async with bulkhead:` in async code. `bulkhead.active` and `bulkhead.waiting` report the current load.

```python
from concurrent.futures import ThreadPoolExecutor
from python_wrap.bulkhead import Bulkhead

db = Bulkhead(3, timeout=5, name="db")

def query(n: int) -> int:
    with db:
        return n * 2

with ThreadPoolExecutor(10) as pool:
    assert list(pool.map(query, range(10))) == [n * 2 for n in range(10)]
assert db.active == 0
```

`BulkheadFull` is a `PythonWrapError`, so it composes with the other decorators, for example [`fallback`](../resilience/fallback.md) to serve a degraded answer when the bulkhead is full, or [`retry`](../resilience/retry.md) to try again a little later.

## limit_concurrency

The decorator form for the common case "at most N of these at once": each decorated function gets its own `Bulkhead(n)` with no queue limit and no timeout, available as `.bulkhead`.

```python
import asyncio
from python_wrap.bulkhead import limit_concurrency

@limit_concurrency(3)
async def crawl(url: str) -> str:
    await asyncio.sleep(0)
    return url.upper()

async def main() -> None:
    pages = await asyncio.gather(*(crawl(f"site{i}") for i in range(10)))
    assert pages[0] == "SITE0"
    assert crawl.bulkhead.max_concurrent == 3

asyncio.run(main())
```

It works on methods too, including under a `@classmethod` stacked above it, and type checkers (mypy and pyright) keep the signature and the `.bulkhead` attribute there. On a method, the limit is shared by every instance (it belongs to the function). For a limit shared by several functions, or for `max_waiting` / `timeout`, create a `Bulkhead` yourself.

## Events

| Event | Fields | Emitted when |
| --- | --- | --- |
| `bulkhead.rejected` | `active`, `waiting` | A call is refused with `BulkheadFull`; the fields are the counts at that moment. |

See [Metrics](../observability/metrics.md).

## Testing

Waiting for a slot (the bulkhead `timeout`) runs on **real time**. `fast_mode()` and `fake_clock()` do not shorten it, on either the sync or the async side.

## Gotchas

!!! warning "Threads and event loops have separate limits"
    Sync callers share a `threading.BoundedSemaphore`; async callers use one `asyncio.Semaphore` per event loop (asyncio primitives cannot be shared across loops). So the cap applies separately to threads and to each loop. If one program uses both sides, give each side its own bulkhead. Never use `with bulkhead:` inside a coroutine: waiting for a slot there would block the event loop.

!!! warning "A bulkhead inside a global limit can starve"
    Suppose `map_async(fetch, urls, limit=10)` calls a function that waits for a per-host `Bulkhead(2)`. A task holds one of the 10 global slots *while it queues for the bulkhead*. If most URLs hit the same host, ten tasks sit on global slots, two work and eight wait, so requests to every other host are blocked behind them (and with a `timeout` or `max_waiting` the waiters may be rejected instead). Two ways out:

    * Put the bulkhead outermost per host: acquire it *before* starting the work that takes a global slot, or group the items by host and run one `map_async` per host (each with `limit` equal to that host's bulkhead size).
    * Size the limits so each per-host limit is at most the global one and the global limit is at least the sum of all the per-host limits, so a global slot is never held for a bulkhead slot that cannot be free.

## See also

- [Async helpers](async-helpers.md), [Rate limits](rate-limits.md)
- [API reference](../../api/bulkhead.md)
