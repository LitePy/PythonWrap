# DataLoader-style batching: ending N+1 queries

## The problem

You render a list of 20 posts, and each post needs its author. The natural code is the problem:

```text
for post in posts:
    author = get_author(post.author_id)    # 20 queries
```

This is the "N+1" query problem, and it appears everywhere: ORMs, GraphQL resolvers, templates, serializers. Fixing it by hand means restructuring the code so someone collects all ids first, loads them in one query and passes the results back down, and that couples every layer to the loading strategy.

[`batch_calls`](../features/caching/batching.md) is the DataLoader pattern: write **one bulk function** that takes a list of ids, and call it **one item at a time** from anywhere. Calls made close together are collected into a single bulk call, and each caller gets its own result.

## The code

```python
import asyncio
from typing import Any

from python_wrap.batching import batch_calls

QUERIES: list[str] = []
AUTHORS = {i: {"id": i, "name": f"author{i}"} for i in range(1, 6)}
POSTS = [{"id": n, "author_id": n % 5 + 1, "title": f"post {n}"} for n in range(20)]


@batch_calls(max_size=100, max_wait=0.005)
async def get_author(ids: list[int]) -> dict[int, dict[str, Any]]:
    """The one place that talks to the database. Receives distinct ids."""
    QUERIES.append(f"SELECT * FROM authors WHERE id IN ({', '.join(map(str, ids))})")
    await asyncio.sleep(0.001)
    return {i: AUTHORS[i] for i in ids if i in AUTHORS}  # missing ids get a BatchError


async def render_post(post: dict[str, Any]) -> str:
    author = await get_author(post["author_id"])  # looks like one query per post...
    return f"{post['title']} by {author['name']}"


async def render_all(posts: list[dict[str, Any]]) -> list[str]:
    return list(await asyncio.gather(*(render_post(p) for p in posts)))  # ...but is 1 in total


async def amain() -> None:
    lines = await render_all(POSTS)
    assert lines[0] == "post 0 by author1"
    print(len(lines), "posts rendered with", len(QUERIES), "query:")
    print(" ", QUERIES[0])
    assert len(QUERIES) == 1  # 20 posts, 5 distinct authors, one query

    # A second wave goes out as a second batch.
    await asyncio.gather(get_author(1), get_author(2))
    assert len(QUERIES) == 2


def main() -> None:
    asyncio.run(amain())


main()
```

## How it works

* **`@batch_calls(max_size=100, max_wait=0.005)`** turns `get_author(ids: list[int])` into a function you call as `get_author(42)`. The first call opens a *window*. The batch is sent when `max_wait` seconds pass (here 5 ms) or when `max_size` distinct items are queued, whichever comes first.
* All 20 `render_post` tasks run in the same event-loop tick via `asyncio.gather`, so they all join one window: **one query for 20 posts**, and only the 5 distinct author ids are sent (duplicates in a window share one slot and one result).
* **The output contract.** The bulk function returns either a *list* with one result per input id in the same order, or a *dict* keyed by id. With a dict, an id that is missing makes **only that caller** get a `BatchError`; the others succeed. If the function raises, every caller in that batch gets the exception.
* The calling code (`render_post`) is written as if there were no batching, so it can stay in the layer where it belongs.

### The same thing with threads

A sync function is batched across threads. This is useful for thread-per-request servers (Flask/Gunicorn threads, Django with threaded workers).

```python
from concurrent.futures import ThreadPoolExecutor

from python_wrap.batching import batch_calls

queries = []


@batch_calls(max_size=4, max_wait=0.05)
def get_price(skus: list[str]) -> dict[str, float]:
    queries.append(sorted(skus))  # one "SELECT ... WHERE sku IN (...)"
    return {sku: float(len(sku)) for sku in skus}


with ThreadPoolExecutor(4) as pool:
    prices = list(pool.map(get_price, ["a", "bb", "ccc", "dddd"]))

assert prices == [1.0, 2.0, 3.0, 4.0]
assert queries == [["a", "bb", "ccc", "dddd"]]
```

There is no timer thread. The first caller of a window waits up to `max_wait` and then runs the batch in its own thread; a caller that fills the batch to `max_size` runs it at once.

## Pitfalls

!!! warning "A lone sequential caller pays the window"
    If a single thread calls the sync loader in a loop, each call waits up to `max_wait` for company that never arrives. Batching needs *concurrent* callers. Never call a sync batched function from `async def` code (it blocks the event loop); decorate an `async def` instead.

* **Return values must line up.** A list must have exactly one item per input id (otherwise every caller gets `BatchError`); a dict may omit ids (those callers get `BatchError`) but extra keys are ignored. Strings, `None` and generators are rejected: return a `list` or `dict`.
* **Items must be hashable**, or pass `key=lambda request: request.id` to say what identifies an item (it is used for deduplication and for dict lookups).
* **Choose `max_wait` to match your latency budget.** 1 to 10 ms is typical. It is added to the latency of the *first* caller of each window.
* **Cancellation is safe.** A cancelled caller removes its item from an unsent batch (unless someone else wants the same key), and cancelling one caller never cancels the batch for the others.
* **It is not a cache.** The same id requested in a later window is queried again. Stack `memoize(ttl=...)` on top if you also want results remembered: `@memoize(ttl=30)` above `@batch_calls()` caches per id and batches the misses.
* `await get_author.aflush()` sends everything queued right now (end of a request, shutdown); `get_author.pending` is the number of items waiting.
* On methods, each instance gets its own batches, so two repositories are never mixed into one bulk call.

## Test it

The bulk function is the only thing that touches the database, so a test counts how many times it was called. Wrap it with `spy` to count without changing behaviour, or, as here, record queries in the fake.

```python
import asyncio
from typing import Any

from python_wrap.batching import batch_calls

QUERIES: list[str] = []
AUTHORS = {i: {"id": i, "name": f"author{i}"} for i in range(1, 6)}
POSTS = [{"id": n, "author_id": n % 5 + 1, "title": f"post {n}"} for n in range(20)]


@batch_calls(max_size=100, max_wait=0.005)
async def get_author(ids: list[int]) -> dict[int, dict[str, Any]]:
    """The one place that talks to the database. Receives distinct ids."""
    QUERIES.append(f"SELECT * FROM authors WHERE id IN ({', '.join(map(str, ids))})")
    await asyncio.sleep(0.001)
    return {i: AUTHORS[i] for i in ids if i in AUTHORS}  # missing ids get a BatchError


async def render_post(post: dict[str, Any]) -> str:
    author = await get_author(post["author_id"])  # looks like one query per post...
    return f"{post['title']} by {author['name']}"


async def render_all(posts: list[dict[str, Any]]) -> list[str]:
    return list(await asyncio.gather(*(render_post(p) for p in posts)))  # ...but is 1 in total

import pytest

from python_wrap.errors import BatchError
from python_wrap.testing import spy


def test_twenty_posts_one_query() -> None:
    QUERIES.clear()
    lines = asyncio.run(render_all(POSTS))
    assert len(lines) == 20
    assert len(QUERIES) == 1
    assert "IN (1, 2, 3, 4, 5)" in QUERIES[0]  # five distinct authors, sent once each


def test_one_missing_row_only_fails_its_own_caller() -> None:
    async def scenario() -> list[object]:
        return list(await asyncio.gather(get_author(1), get_author(999), return_exceptions=True))

    found, missing = asyncio.run(scenario())
    assert found == {"id": 1, "name": "author1"}
    assert isinstance(missing, BatchError)


def test_flush_sends_the_queue_immediately() -> None:
    @batch_calls(max_wait=60)  # a long window...
    async def slow_window(ids: list[int]) -> list[int]:
        return [i * 10 for i in ids]

    async def scenario() -> list[int]:
        tasks = [asyncio.create_task(slow_window(i)) for i in range(3)]
        await asyncio.sleep(0)  # let the tasks queue their items
        assert slow_window.pending == 3
        await slow_window.aflush()  # ...cut short: do not wait a minute
        return [t.result() for t in tasks]

    assert asyncio.run(scenario()) == [0, 10, 20]


def test_a_spy_counts_bulk_calls() -> None:
    async def bulk(ids: list[int]) -> list[int]:
        return [i * 10 for i in ids]

    calls = spy(bulk)  # records every call, changes nothing
    loader = batch_calls(max_wait=0.001)(calls)

    async def scenario() -> list[int]:
        return list(await asyncio.gather(loader(1), loader(2), loader(1)))

    assert asyncio.run(scenario()) == [10, 20, 10]
    assert calls.call_count == 1
    assert calls.calls[0].args == ([1, 2],)  # distinct ids, in arrival order


# pytest collects the test_ functions itself; this loop only lets the page run them directly.
for _name, _test in list(globals().items()):
    if _name.startswith("test_"):
        _test()
```
