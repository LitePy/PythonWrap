# An async crawler and bulk API fetch

## The problem

You need to fetch thousands of URLs (a crawl, a nightly sync, "enrich these 50,000 rows from an API"). Plain `asyncio.gather` starts everything at once and exhausts sockets or gets you banned. A hand-written `Semaphore` plus a queue plus error handling is forty lines you rewrite every time. You also want:

- a **global** cap on concurrent requests,
- a **per-host** cap, so one slow site cannot absorb every slot and nobody is hammered,
- failures **collected**, not fatal to the whole batch,
- a way to ask several **mirrors** and take the first good answer.

## The code

`map_async` is the global cap, `Bulkhead` is the per-host cap, `race` handles mirrors and `run_sync` is the sync entry point. The network is simulated with `asyncio.sleep` so the page runs offline.

```python
import asyncio
from collections import Counter
from urllib.parse import urlsplit

from python_wrap.aio import map_async, race, run_sync
from python_wrap.bulkhead import Bulkhead

# --- simulated network -------------------------------------------------------------

LATENCY = {"fast.example": 0.002, "slow.example": 0.01, "mirror-a.example": 0.02}
in_flight: Counter[str] = Counter()
peak: Counter[str] = Counter()


async def http_get(url: str) -> str:
    host = urlsplit(url).netloc
    in_flight[host] += 1
    peak[host] = max(peak[host], in_flight[host])
    try:
        await asyncio.sleep(LATENCY.get(host, 0.005))
        if "broken" in url:
            raise ConnectionError(f"cannot reach {url}")
        return f"<html>{url}</html>"
    finally:
        in_flight[host] -= 1


# --- the crawler -------------------------------------------------------------------

PER_HOST = 2
_host_bulkheads: dict[str, Bulkhead] = {}


def bulkhead_for(host: str) -> Bulkhead:
    if host not in _host_bulkheads:
        _host_bulkheads[host] = Bulkhead(PER_HOST, timeout=5, name=host)
    return _host_bulkheads[host]


async def fetch(url: str) -> str:
    async with bulkhead_for(urlsplit(url).netloc):
        return await http_get(url)


async def fetch_from_mirrors(path: str, mirrors: list[str]) -> str:
    """First mirror to answer wins; the others are cancelled."""
    return await race(*(fetch(f"https://{m}{path}") for m in mirrors), timeout=2)


async def crawl(
    urls: list[str], *, limit: int = 8
) -> tuple[dict[str, str], dict[str, BaseException]]:
    results = await map_async(fetch, urls, limit=limit, return_exceptions=True)
    pages: dict[str, str] = {}
    errors: dict[str, BaseException] = {}
    for url, result in zip(urls, results, strict=True):
        if isinstance(result, BaseException):
            errors[url] = result
        else:
            pages[url] = result
    return pages, errors


async def amain() -> None:
    urls = [f"https://{host}/page/{i}" for i in range(10) for host in LATENCY]
    urls.append("https://fast.example/broken")

    pages, errors = await crawl(urls)
    print(f"fetched {len(pages)} pages, {len(errors)} failed")
    assert len(pages) == 30
    assert list(errors) == ["https://fast.example/broken"]
    assert all(count <= PER_HOST for count in peak.values()), peak
    print("peak concurrency per host:", dict(peak))

    body = await fetch_from_mirrors("/file", ["mirror-a.example", "fast.example"])
    assert body == "<html>https://fast.example/file</html>"
    print("mirror race won by the fast mirror")


def main() -> None:
    run_sync(amain())  # the entry point of a sync script


main()
```

## How it works

* **`map_async(fetch, urls, limit=8, return_exceptions=True)`** runs `fetch` on every URL with at most 8 in flight. It reads `urls` lazily (an item is pulled only when a slot frees), so a generator of a million URLs never becomes a million tasks. Results come back in input order; with `return_exceptions=True` a failure is an item in the list rather than an exception that cancels everything else.
* **`Bulkhead(PER_HOST, timeout=5)`** per host. `async with bulkhead:` takes a slot or waits for one; if a caller waits more than `timeout` seconds it gets `BulkheadFull` (a `PythonWrapError`), which the crawler records as that URL's error like any other. Use `max_waiting=` to bound the queue instead of the time.
* **`race(*coros, timeout=2)`** starts every mirror, returns the first **successful** result and cancels and awaits the losers. A mirror that fails does not end the race. If all fail, `RaceFailed` carries every exception in `.exceptions`.
* **`run_sync(amain())`** is `asyncio.run` that also accepts a function and its arguments. It refuses to run inside an already-running loop (rather than hanging), which is what you want from a script entry point.

For inputs too large to hold the results, stream them. `amap_iter` yields results as they are ready and cancels the in-flight calls if you stop early:

```python
import asyncio
from contextlib import aclosing

from python_wrap.aio import amap_iter


async def fetch(n: int) -> str:
    await asyncio.sleep(0)
    return f"page {n}"


async def main() -> None:
    seen = 0
    async with aclosing(amap_iter(fetch, range(1_000_000), limit=16)) as pages:
        async for page in pages:
            seen += 1
            if seen == 100:  # stop early: the rest are never fetched
                break
    assert seen == 100


asyncio.run(main())
```

If you already have a fixed list of coroutines, `gather_limit(*coros, limit=8)` is `asyncio.gather` with a cap.

## Pitfalls

!!! warning "Two limits can block each other"
    A task occupies one of the global `limit` slots while it waits for its host's bulkhead. If your URL list is sorted by host and the global limit is 8 but the per-host limit is 2, six slots sit idle waiting on one host while other hosts have no work. Interleave URLs from different hosts (as the demo does), keep the global limit close to `hosts * per_host`, or sort by round-robin. Symptoms: throughput far below `limit`.

!!! warning "`race` is for idempotent work"
    The losers are cancelled mid-flight and may already have done part of their job. Racing mirrors for a `GET` is fine; racing two `POST`s is not.

* Without `return_exceptions=True` the **first** exception cancels the rest of the batch and is raised. That is right for "all or nothing" work and wrong for a crawl.
* `return_exceptions=True` also returns `asyncio.CancelledError` objects if a task was cancelled; check for `BaseException`, as the demo does, if that matters.
* Bulkheads keep one semaphore per event loop, so creating them at module level is safe even if you call `asyncio.run` several times, but the cap then applies **per loop**. Do not mix `with bulkhead:` (threads) and `async with bulkhead:` for the same resource; give each side its own.
* `timeout=` on a bulkhead measures time spent **waiting** for a slot, not the request itself. Put a real timeout on the HTTP client or wrap the request in `python_wrap.timeout`.
* Add `retry` to `fetch` (outside the `async with`, so a retry waits for a slot again) if transient errors are expected; see [the resilient client](resilient-http-client.md).

## Test it

Async code is testable with `asyncio.run` inside plain test functions, so no pytest plugin is needed. The structure to check is the **limits**: the peak concurrency, the error handling and the mirror winner.

```python
import asyncio
from collections import Counter
from urllib.parse import urlsplit

from python_wrap.aio import map_async, race, run_sync
from python_wrap.bulkhead import Bulkhead

# --- simulated network -------------------------------------------------------------

LATENCY = {"fast.example": 0.002, "slow.example": 0.01, "mirror-a.example": 0.02}
in_flight: Counter[str] = Counter()
peak: Counter[str] = Counter()


async def http_get(url: str) -> str:
    host = urlsplit(url).netloc
    in_flight[host] += 1
    peak[host] = max(peak[host], in_flight[host])
    try:
        await asyncio.sleep(LATENCY.get(host, 0.005))
        if "broken" in url:
            raise ConnectionError(f"cannot reach {url}")
        return f"<html>{url}</html>"
    finally:
        in_flight[host] -= 1


# --- the crawler -------------------------------------------------------------------

PER_HOST = 2
_host_bulkheads: dict[str, Bulkhead] = {}


def bulkhead_for(host: str) -> Bulkhead:
    if host not in _host_bulkheads:
        _host_bulkheads[host] = Bulkhead(PER_HOST, timeout=5, name=host)
    return _host_bulkheads[host]


async def fetch(url: str) -> str:
    async with bulkhead_for(urlsplit(url).netloc):
        return await http_get(url)


async def fetch_from_mirrors(path: str, mirrors: list[str]) -> str:
    """First mirror to answer wins; the others are cancelled."""
    return await race(*(fetch(f"https://{m}{path}") for m in mirrors), timeout=2)


async def crawl(
    urls: list[str], *, limit: int = 8
) -> tuple[dict[str, str], dict[str, BaseException]]:
    results = await map_async(fetch, urls, limit=limit, return_exceptions=True)
    pages: dict[str, str] = {}
    errors: dict[str, BaseException] = {}
    for url, result in zip(urls, results, strict=True):
        if isinstance(result, BaseException):
            errors[url] = result
        else:
            pages[url] = result
    return pages, errors

import asyncio

import pytest

from python_wrap.aio import RaceFailed
from python_wrap.testing import eventually


def test_global_and_per_host_limits() -> None:
    peak.clear()
    urls = [f"https://{h}/p{i}" for i in range(12) for h in LATENCY]
    pages, errors = asyncio.run(crawl(urls, limit=4))
    assert len(pages) == len(urls) and not errors
    assert max(peak.values()) <= PER_HOST
    assert sum(1 for v in peak.values() if v) <= len(LATENCY)


def test_failures_are_collected_not_raised() -> None:
    pages, errors = asyncio.run(crawl(["https://fast.example/ok", "https://fast.example/broken"]))
    assert list(pages) == ["https://fast.example/ok"]
    assert isinstance(errors["https://fast.example/broken"], ConnectionError)


def test_mirror_race_survives_a_broken_mirror() -> None:
    # The first contestant fails at once; the race carries on and the slow site wins.
    async def scenario() -> str:
        async def broken() -> str:
            raise ConnectionError("mirror down")

        return await race(broken(), fetch("https://slow.example/file"), timeout=2)

    assert asyncio.run(scenario()) == "<html>https://slow.example/file</html>"


def test_all_mirrors_down() -> None:
    with pytest.raises(RaceFailed) as info:
        asyncio.run(fetch_from_mirrors("/broken", ["fast.example", "slow.example"]))
    assert len(info.value.exceptions) == 2


def test_no_request_is_left_running() -> None:
    def nothing_in_flight() -> None:
        assert not any(in_flight.values())

    async def scenario() -> None:
        await crawl([f"https://slow.example/{i}" for i in range(6)])
        await eventually(nothing_in_flight, timeout=1)  # polls until the assertion passes

    asyncio.run(scenario())


# pytest collects the test_ functions itself; this loop only lets the page run them directly.
for _name, _test in list(globals().items()):
    if _name.startswith("test_"):
        _test()
```

!!! tip "Pytest-asyncio"
    With `pytest-asyncio` (or `anyio`) you can write `async def test_...` directly and `await` the same calls. The helper `eventually(assertion, timeout=...)` is awaitable and polls an assertion until it passes, which is the clean way to wait for background work in an async test.
