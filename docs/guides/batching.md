# Batching and hedging

Two decorators for calls that go over the network:

| Decorator | One-line behaviour |
| --- | --- |
| [`batch_calls`](#batch_calls) | Many single-item calls made close together become one batched call. |
| [`hedge`](#hedge) | If a call is slow, start a backup attempt; the first success wins. |

## `batch_calls`

You often have code that loads one thing at a time — `await get_user(id)` in many places, many tasks — while the backend is much faster when asked for many things at once (`SELECT ... WHERE id IN (...)`, a bulk API endpoint). `batch_calls` is the DataLoader pattern: write the bulk function once, and call it with one item from everywhere.

=== "Async"

    ```python
    import asyncio
    from python_wrap.batching import batch_calls

    queries = []

    @batch_calls(max_size=100, max_wait=0.005)
    async def get_user(ids: list[int]) -> dict[int, str]:
        queries.append(ids)                        # one round trip for the whole batch
        return {i: f"user{i}" for i in ids}

    async def main() -> None:
        users = await asyncio.gather(get_user(1), get_user(2), get_user(1))
        assert users == ["user1", "user2", "user1"]
        assert queries == [[1, 2]]                 # one call; the duplicate 1 was sent once

    asyncio.run(main())
    ```

=== "Sync (threads)"

    ```python
    from concurrent.futures import ThreadPoolExecutor
    from python_wrap.batching import batch_calls

    queries = []

    @batch_calls(max_size=4, max_wait=0.05)
    def get_price(skus: list[str]) -> list[float]:
        queries.append(sorted(skus))
        return [float(len(s)) for s in skus]       # same order as the input

    with ThreadPoolExecutor(4) as pool:
        prices = list(pool.map(get_price, ["a", "bb", "ccc", "dddd"]))

    assert prices == [1.0, 2.0, 3.0, 4.0]
    assert queries == [["a", "bb", "ccc", "dddd"]]  # the 4th caller filled the batch
    ```

### When a batch is sent

A **window** opens with the first call. The batch is sent when either happens first:

* `max_wait` seconds have passed since the window opened (default 10 ms), or
* the batch holds `max_size` distinct items (default 100).

The next call after that opens a new window. Duplicate items in one window — same key — are sent once and their callers share the result.

### The output contract

The batch function receives a `list` of the **distinct** items, in the order they first arrived. It must return one of:

| Return value | How results are matched |
| --- | --- |
| A sequence (`list`, `tuple`, ...) | Position: result `i` belongs to item `i`. The length must equal the number of items. |
| A mapping (`dict`, ...) | By key: `key(item)` if you passed `key=`, otherwise the item itself. Extra entries are ignored. |

Anything else is a contract violation. What callers see:

| What the batch function did | Who gets what |
| --- | --- |
| Returned a valid result | Each caller gets its own item's result. |
| Raised an exception | **Every** caller in the batch gets that exception. |
| Returned a sequence of the wrong length | **Every** caller gets `BatchError`. |
| Returned a mapping missing some keys | Only callers of the missing items get `BatchError`; the rest get results. |
| Returned something else (`None`, a `str`, a generator, ...) | **Every** caller gets `BatchError`. |

`str` and `bytes` are not accepted as sequences, and iterators are not consumed: return a `list`. `BatchError` is a `PythonWrapError`.

```python
import asyncio
from python_wrap.batching import batch_calls
from python_wrap.errors import BatchError

@batch_calls()
async def lookup(ids: list[int]) -> dict[int, str]:
    return {i: "found" for i in ids if i != 404}

async def main() -> None:
    found, missing = await asyncio.gather(lookup(1), lookup(404), return_exceptions=True)
    assert found == "found"
    assert isinstance(missing, BatchError)        # only this caller fails

asyncio.run(main())
```

### Keys: deduplication and unhashable items

Items must be hashable, or you pass `key=` to say what identifies one. The key is used to deduplicate within a window and to look results up in a returned mapping.

```python
import asyncio
from python_wrap.batching import batch_calls

@batch_calls(key=lambda req: req["id"])
async def resolve(requests: list[dict]) -> dict[int, str]:
    return {r["id"]: r["name"].title() for r in requests}

async def main() -> None:
    assert await resolve({"id": 7, "name": "ada"}) == "Ada"

asyncio.run(main())
```

### Async details

* Callers on the **same event loop** are batched together; batches never span loops.
* The batch runs in its own task. Cancelling one caller does not cancel the batch for the others.
* A caller cancelled **before** its batch is sent removes its item from the batch, unless another caller is waiting for the same key. If every caller goes away, the batch is never sent.
* No tasks are left behind once the batch has been delivered.
* When a call fills the batch to `max_size` and its caller is the batch's only waiter (always the case with `max_size=1`), the batch runs right in the calling task: no extra task, no event-loop round trip.

### Sync details

* Callers are threads; each blocks until its result is ready.
* There is no timer thread. The **first caller of a window** keeps time: it waits up to `max_wait` and then runs the batch in its own thread. A caller that fills the batch to `max_size` runs it immediately in its thread. So the batch function always runs in one of the calling threads (or the thread calling `.flush()`).
* A single thread calling the loader in a loop waits up to `max_wait` per call — batching needs concurrency. Do not call a sync loader from `async` code; it blocks the event loop. Decorate an `async def` instead.

!!! warning "A sync loader is not awaitable"
    Decorating a plain `def` gives a plain function: `loader(1)` blocks and returns the value itself. So `await loader(1)` and `asyncio.create_task(loader(1))` fail (with `TypeError`, after the loop was already blocked for the batch window). The first time a sync loader is called from a running event loop it emits a `RuntimeWarning` saying so. In async code, decorate an `async def` batch function.

### Flushing and inspecting

* `.pending` — how many distinct items are queued and not yet sent.
* `.flush()` (sync) — send every queued batch now, in the calling thread.
* `await .aflush()` (async) — send the batches queued on the running loop now, and wait until every batch on that loop has finished. Useful at the end of a request or before shutdown.

```python
import asyncio
from python_wrap.batching import batch_calls

@batch_calls(max_wait=60)          # a long window...
async def load(ids: list[int]) -> list[int]:
    return [i * 10 for i in ids]

async def main() -> None:
    tasks = [asyncio.create_task(load(i)) for i in range(3)]
    await asyncio.sleep(0)         # let the tasks queue their items
    assert load.pending == 3
    await load.aflush()            # ...cut short
    assert [t.result() for t in tasks] == [0, 10, 20]

asyncio.run(main())
```

### Methods

On a method the batch function is called as `func(self, items)`, and each instance gets its own batches (for a class method, each class). Calls on two instances are never mixed in one batch.

```python
import asyncio
from python_wrap.batching import batch_calls

class UserRepo:
    def __init__(self, table: str) -> None:
        self.table = table

    @batch_calls()
    async def get(self, ids: list[int]) -> list[str]:
        return [f"{self.table}:{i}" for i in ids]

async def main() -> None:
    a, b = UserRepo("a"), UserRepo("b")
    assert await asyncio.gather(a.get(1), b.get(2)) == ["a:1", "b:2"]

asyncio.run(main())
```

!!! note "Static typing"
    The wrapper is typed as `Batched[[Item], Result]` (or `AsyncBatched`), with `.pending`, `.flush()` / `.aflush()`. Pyright types instance, class and static methods exactly. mypy types instance and class methods correctly but binds the first parameter too eagerly for a `@staticmethod` stacked on top, and for unbound access such as `Repo.get(repo, 1)`; prefer a module-level loader there.

## `hedge`

Most calls to a replicated service are fast, but a few hit a slow replica, a GC pause, or a lost packet. A **hedged request** waits a little; if the call is still running, it fires a second, identical call and takes whichever answers first.

!!! warning "Only for idempotent operations"
    A hedged function can run several times for one call, at the same time. Use it for reads and other operations that are safe to repeat. Never hedge payments, emails, counters, or anything else whose side effect must happen once.

```python
import asyncio
from python_wrap.hedging import hedge

attempts = []

@hedge(0.05, max_extra=1)
async def get_price(sku: str) -> float:
    attempts.append(sku)
    if len(attempts) == 1:
        await asyncio.sleep(10)      # the first replica is stuck...
    return 9.99

async def main() -> None:
    assert await get_price("A1") == 9.99   # ...so the backup answers after ~50 ms
    assert len(attempts) == 2

asyncio.run(main())
```

### How it decides

1. Start the call.
2. If no attempt has finished after `delay` seconds, start another one (up to `max_extra` extra attempts in total, each `delay` after the previous one).
3. The first attempt to **succeed** wins; the rest are cancelled.
4. If an attempt fails with an exception matching `on` (default: any `Exception`) while there are attempts left, the next one starts **immediately** — there is no point waiting out the delay.
5. An exception that does not match `on` is raised at once (and the other attempts are cancelled).
6. If every attempt fails, the last exception is raised.

Pick `delay` near the latency percentile you want to cut — for example the p95 of the call. Then only about 5% of calls send an extra request.

```python
import asyncio
from python_wrap.hedging import hedge

calls = []

@hedge(10, max_extra=2, on=ConnectionError)
async def fetch() -> str:
    calls.append(1)
    if len(calls) < 3:
        raise ConnectionError("replica down")
    return "ok"

async def main() -> None:
    assert await fetch() == "ok"   # failures hedge immediately: no 10 s waits
    assert len(calls) == 3

asyncio.run(main())
```

### Async: cancellation guarantees

Each attempt is a task. When the wrapper returns or raises, every other attempt has been cancelled **and awaited**, so no tasks are left running. Cancelling the caller cancels all attempts. Errors raised by losing attempts while they are being cancelled are discarded. Context variables are copied into each attempt.

### Sync: attempts cannot be cancelled

For a regular function each attempt runs in its own daemon thread and the caller blocks until one succeeds. **Python threads cannot be cancelled**: losing attempts keep running in the background until they finish, and their results are thrown away. Their side effects still happen, and they hold their resources (a connection, a lock) until done. Make sure the function has its own timeout, so a stuck attempt does not run forever.

```python
from python_wrap.hedging import hedge

@hedge(0.01)
def lookup(key: str) -> str:
    return key.upper()

assert lookup("a") == "A"
```

`hedge` keeps the decorated function's signature and type, so it stacks with other decorators and works on methods.
