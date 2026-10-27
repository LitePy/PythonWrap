# Async toolkit and bulkheads

Small helpers for the things every asyncio project ends up hand-writing: bounded
`gather`, bounded `map`, racing, calling async code from sync code, and capping how many
calls hit a fragile dependency at once.

| Helper | One-line behaviour |
| --- | --- |
| [`gather_limit`](#gather_limit) | `asyncio.gather`, but at most `limit` run at once. |
| [`map_async`](#map_async-and-amap_iter) | Apply an async function to every item, `limit` at a time; get a list. |
| [`amap_iter`](#map_async-and-amap_iter) | Same, as an async generator: results stream out. |
| [`race`](#race) | First success wins; the others are cancelled. |
| [`run_sync`](#run_sync) | Run a coroutine from sync code (never inside a running loop). |
| [`Bulkhead`](#bulkhead) | Cap concurrent calls, bound the queue, fail fast with `BulkheadFull`. |
| [`limit_concurrency`](#limit_concurrency) | Decorator: a private `Bulkhead(n)` per function. |

The async helpers live in `python_wrap.aio`, the bulkhead in `python_wrap.bulkhead`.

!!! note "No task is left behind"
    Every helper owns the tasks it starts. When it returns, raises, is cancelled or (for
    `amap_iter`) is closed early, all of them have been cancelled *and awaited*. Nothing
    keeps running in the background, and you never see "Task exception was never
    retrieved" or "coroutine was never awaited" warnings from them.

## `gather_limit`

Like `asyncio.gather`, but coroutines are started only when one of the `limit` slots is
free. Results come back in input order.

```python
import asyncio
from python_wrap.aio import gather_limit

running = 0
peak = 0

async def download(n: int) -> str:
    global running, peak
    running += 1
    peak = max(peak, running)
    await asyncio.sleep(0.001)
    running -= 1
    return f"file-{n}"

async def main() -> None:
    files = await gather_limit(*(download(n) for n in range(20)), limit=5)
    assert files[:2] == ["file-0", "file-1"]
    assert peak == 5

asyncio.run(main())
```

On the first exception the other awaitables are cancelled, coroutines that never started
are closed, and the exception is raised. Pass `return_exceptions=True` to collect errors
in the list instead:

```python
import asyncio
from python_wrap.aio import gather_limit

async def parse(text: str) -> int:
    return int(text)

async def main() -> None:
    results = await gather_limit(parse("1"), parse("x"), limit=2, return_exceptions=True)
    assert results[0] == 1
    assert isinstance(results[1], ValueError)

asyncio.run(main())
```

Tasks and futures are accepted too, but they are already running when you pass them, so
`limit` cannot delay them; pass coroutines to get the cap.

## `map_async` and `amap_iter`

`map_async(fn, items, limit=...)` calls `fn` on every item with at most `limit` calls in
flight, and returns the list of results. `items` may be a normal or an async iterable,
and it is read **lazily**: an item is pulled only when a slot is free, so a generator of
a million rows never sits in memory as a million tasks.

```python
import asyncio
from python_wrap.aio import map_async

async def rows():
    for i in range(100):
        yield {"id": i}

async def save(row: dict[str, int]) -> int:
    await asyncio.sleep(0)
    return row["id"]

async def main() -> None:
    ids = await map_async(save, rows(), limit=8)
    assert ids == list(range(100))

asyncio.run(main())
```

`ordered=False` returns results in completion order instead. If a call fails, the others
are cancelled and the error is raised (unless `return_exceptions=True`).

`amap_iter` is the streaming version: it yields results as they are ready, which also
works with infinite inputs.

```python
import asyncio
from contextlib import aclosing
from itertools import count
from python_wrap.aio import amap_iter

async def square(n: int) -> int:
    await asyncio.sleep(0)
    return n * n

async def main() -> None:
    found = []
    async with aclosing(amap_iter(square, count(), limit=4)) as squares:
        async for value in squares:
            if value > 50:
                break
            found.append(value)
    assert found == [0, 1, 4, 9, 16, 25, 36, 49]

asyncio.run(main())
```

* `ordered=True` (default) yields in input order. Results that finish early wait in a
  buffer, and buffered results count towards `limit`, so memory stays bounded; a slow
  item holds back the ones after it. An error is raised when its turn comes.
* `ordered=False` yields each result as soon as it finishes.

Breaking out of the loop, calling `aclose()` or cancelling the consumer cancels and
awaits every call still in flight. Wrap the generator in `contextlib.aclosing` (as above)
so this happens immediately instead of whenever the generator is garbage-collected. The
input iterable is yours: `amap_iter` does not close it.

## `race`

Start every awaitable and return the first **successful** result. The losers are
cancelled and awaited before `race` returns. A contestant that raises does not end the
race; the others keep going.

```python
import asyncio
from python_wrap.aio import race

async def from_cache() -> str:
    raise LookupError("cache miss")

async def from_replica() -> str:
    await asyncio.sleep(0.01)
    return "replica"

async def from_primary() -> str:
    await asyncio.sleep(1)
    return "primary"

async def main() -> None:
    assert await race(from_cache(), from_replica(), from_primary(), timeout=5) == "replica"

asyncio.run(main())
```

* With `timeout=`, `TimeoutExpired` (a `TimeoutError`) is raised if nobody succeeds in
  time.
* If every contestant fails, `RaceFailed` is raised with all the errors, in input order,
  in `.exceptions`. On Python 3.11+ `RaceFailed` is also an `ExceptionGroup`, so
  `except* LookupError:` works; on 3.10 it is a plain `PythonWrapError`. Catching
  `RaceFailed` works on every version.

```python
import asyncio
from python_wrap.aio import RaceFailed, race

async def broken(name: str) -> str:
    raise ConnectionError(name)

async def main() -> None:
    try:
        await race(broken("a"), broken("b"))
    except RaceFailed as exc:
        assert [str(e) for e in exc.exceptions] == ["a", "b"]

asyncio.run(main())
```

`race` is for idempotent work: the losers may already have done part of their job when
they are cancelled.

## `run_sync`

Run async code from a sync entry point (a CLI command, a script, a test helper). It is
`asyncio.run` with a friendlier call form:

```python
import asyncio
from python_wrap.aio import run_sync

async def fetch_total(a: int, b: int) -> int:
    await asyncio.sleep(0)
    return a + b

assert run_sync(fetch_total, 2, 3) == 5
assert run_sync(fetch_total(4, 5)) == 9
```

There is deliberately **no nested-event-loop magic**. Called while an event loop is
running in the current thread, `run_sync` raises `RuntimeError` (and closes the coroutine
so you get no "never awaited" warning). Inside async code, `await` instead; from sync
code that runs inside a loop, move the sync code to a thread with `asyncio.to_thread`.

## `Bulkhead`

A bulkhead protects something with limited capacity (a connection pool, a rate-limited
API, a CPU-heavy section) by capping how many calls use it at once, and by refusing to
build an unbounded queue in front of it.

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

| Argument | Meaning |
| --- | --- |
| `max_concurrent` | Calls allowed to run at once. |
| `max_waiting` | Callers allowed to queue for a slot. `None` = unlimited, `0` = never wait. |
| `timeout` | Seconds a queued caller waits before giving up with `BulkheadFull`. |
| `name` | Shown in error messages and `repr`. |

One bulkhead can guard several functions and blocks at once. Use it as a decorator (sync
or `async def`), as `with bulkhead:` in threaded code, or as `async with bulkhead:` in
async code. `bulkhead.active` and `bulkhead.waiting` report the current load.

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

!!! warning "Threads and event loops have separate limits"
    Sync callers share a `threading.BoundedSemaphore`; async callers use one
    `asyncio.Semaphore` per event loop (asyncio primitives cannot be shared across loops).
    So the cap applies separately to threads and to each loop. If one program uses both
    sides, give each side its own bulkhead. Never use `with bulkhead:` inside a coroutine:
    waiting for a slot there would block the event loop.

`BulkheadFull` is a `PythonWrapError`, so it composes with the other decorators, for
example `fallback` to serve a degraded answer when the bulkhead is full, or `retry` to
try again a little later.

## `limit_concurrency`

The decorator form for the common case "at most N of these at once": each decorated
function gets its own `Bulkhead(n)` with no queue limit and no timeout, available as
`.bulkhead`.

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

On a method, the limit is shared by every instance (it belongs to the function). For a
limit shared by several functions, or for `max_waiting` / `timeout`, create a `Bulkhead`
yourself.
