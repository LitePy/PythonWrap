# FastAPI: caching, thundering herds and per-user rate limits

## The problem

A web endpoint reads a profile, a feed or a price list from a slow backend. Under load three things go wrong:

1. The same expensive lookup runs on every request.
2. When a cached entry expires, **hundreds of requests miss at the same moment** and all hit the database (the thundering herd, or cache stampede).
3. One noisy user uses everybody's capacity.

python-wrap fixes each with one decorator on a plain function, and the pieces are framework-free: the same functions work in FastAPI, Flask, Starlette or a worker. This page first shows the framework-free code that you can run and test, then the thin FastAPI layer on top.

## The framework-free core

```python
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

from python_wrap import RateLimitExceeded, memoize, rate_limit, singleflight
from python_wrap.testing import fake_clock

DB_QUERIES: list[int] = []


# Order matters: memoize outside (check the cache first), singleflight inside (only one
# miss per key goes to the database at a time).
@memoize(ttl=60, maxsize=1000)
@singleflight
def load_profile(user_id: int) -> dict[str, object]:
    DB_QUERIES.append(user_id)
    time.sleep(0.03)  # a slow query
    return {"id": user_id, "name": f"user{user_id}"}


# Async code: memoize already shares one in-flight task between concurrent callers.
@memoize(ttl=60)
async def load_feed(user_id: int) -> list[str]:
    DB_QUERIES.append(-user_id)
    await asyncio.sleep(0.02)
    return [f"post {n} for {user_id}" for n in range(3)]


@rate_limit(3, 60, key=lambda user, *args, **kwargs: user)  # 3 searches/minute/user
def search(user: str, query: str) -> list[str]:
    return [f"{query} result {n}" for n in range(2)]


def search_endpoint(user: str, query: str) -> tuple[int, dict[str, object]]:
    """What an HTTP layer does with the exceptions: map them to status codes."""
    try:
        return 200, {"results": search(user, query)}
    except RateLimitExceeded as exc:
        return 429, {"error": "slow down", "retry_after": round(exc.retry_after)}


async def amain() -> None:
    feeds = await asyncio.gather(*(load_feed(7) for _ in range(50)))
    assert all(feed == feeds[0] for feed in feeds)
    assert DB_QUERIES.count(-7) == 1  # 50 concurrent requests, one query
    print("async: 50 concurrent feed requests ->", DB_QUERIES.count(-7), "query")


def main() -> None:
    # Install the fake clock before the first call: cache entries remember the clock
    # that was running when they were stored.
    with fake_clock() as clock:
        # Thundering herd: 20 threads miss the cache at the same instant.
        with ThreadPoolExecutor(20) as pool:
            profiles = list(pool.map(load_profile, [42] * 20))
        assert all(p["id"] == 42 for p in profiles)
        assert DB_QUERIES.count(42) == 1
        print("threads: 20 concurrent profile requests ->", DB_QUERIES.count(42), "query")

        # Later calls are served from the cache until the TTL passes.
        load_profile(42)
        assert DB_QUERIES.count(42) == 1
        clock.advance(61)
        load_profile(42)
        assert DB_QUERIES.count(42) == 2
        print("cache info:", load_profile.cache_info())

        asyncio.run(amain())

        # Per-user limits: ana runs out, ben is unaffected.
        statuses = [search_endpoint("ana", "cats")[0] for _ in range(4)]
        assert statuses == [200, 200, 200, 429]
        assert search_endpoint("ben", "dogs")[0] == 200
        print("ana's statuses:", statuses, "| ben: 200")


main()
```

### How it works

* **`@memoize(ttl=60, maxsize=1000)`** keeps results per argument for 60 seconds, evicting least-recently-used entries past 1000. `load_profile.cache_info()` reports hits, misses and size; `cache_delete(user_id)` and `cache_clear()` invalidate.
* **`@singleflight`** lets only one call per key run at a time; every concurrent caller with the same key waits for that call and receives the same result (or the same exception). Stacked *under* `memoize`, the order is "check the cache first, and if it misses, coalesce the misses".
* For `async def` you do not need `singleflight`: `memoize` already shares one in-flight task between concurrent awaits of a missing key, as the `load_feed` demo shows (50 requests, 1 query).
* **`@rate_limit(3, 60, key=...)`** allows 3 calls per 60 seconds per key. `key=` receives the call's arguments and returns the bucket. Over the limit it raises `RateLimitExceeded`, whose `retry_after` says when a slot frees; rejected calls do not use up budget.

## The FastAPI layer

The handlers stay thin: translate HTTP into function calls and exceptions into status codes.

<!-- skip -->
```python
import math

from fastapi import Depends, FastAPI, Header, HTTPException

from python_wrap import RateLimitExceeded, memoize, rate_limit, singleflight

app = FastAPI()


# A sync service function: FastAPI runs `def` endpoints in a thread pool, so the
# herd is made of threads and the sync singleflight coalesces them.
@memoize(ttl=60, maxsize=1000)
@singleflight
def load_profile(user_id: int) -> dict:
    return db.query_profile(user_id)  # slow


# An async service function: memoize alone dedupes concurrent misses.
@memoize(ttl=30)
async def load_feed(user_id: int) -> list[str]:
    return await db.query_feed(user_id)


@rate_limit(30, 60, key=lambda user: user)  # 30 requests/minute per user
def consume_quota(user: str) -> None: ...


def current_user(x_user: str = Header()) -> str:
    return x_user  # replace with your real authentication


def enforce_quota(user: str = Depends(current_user)) -> str:
    try:
        consume_quota(user)
    except RateLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail="slow down",
            headers={"Retry-After": str(math.ceil(exc.retry_after))},
        ) from exc
    return user


@app.get("/profile/{user_id}")
def read_profile(user_id: int, user: str = Depends(enforce_quota)) -> dict:
    return load_profile(user_id)


@app.get("/feed")
async def read_feed(user: str = Depends(enforce_quota)) -> list[str]:
    return await load_feed(int(user))
```

Two details make this work. `memoize` and `singleflight` use `functools.wraps`, so FastAPI still sees the original signature of anything you decorate. And the rate limiter is applied to a small `consume_quota` function rather than to the endpoint, because its job is to count and raise: the HTTP concern (`429`, `Retry-After`) lives in one dependency that every route can reuse.

## Pitfalls

!!! warning "Cached values are shared objects"
    `load_profile(42)` returns the **same dict** to every caller. If one handler mutates it, every later request sees the change. Return immutable data (tuples, frozen dataclasses, Pydantic models with `frozen=True`) or copy before you modify.

!!! warning "Everything here is per process"
    `uvicorn --workers 4` gives you four independent caches and four independent rate-limit counters, so a "30 per minute" limit is really up to 120. That is fine for caches (each worker warms its own) and for protecting a single process, but it is not a global quota. Use a shared store (Redis, your API gateway) when the limit is a contract.

* **Do not memoize functions that take a `Request`, a DB session or other unhashable or per-request arguments.** Decorate the inner service function that takes plain values (`user_id`), or pass `key=lambda request, user_id: user_id`.
* **Errors are not cached.** If `load_profile` raises, the next request tries again, which is usually what you want. Wrap it in `fallback` or `retry` if not.
* **TTL counts from the moment an entry was stored**, not from its last read, so a hot key is recomputed every 60 s. That recomputation is exactly the moment `singleflight` protects. Keys stored together also expire together.
* **Use `cache_delete(...)` after a write** to the underlying data, or users see stale values for up to `ttl` seconds.
* Sync singleflight blocks threads while it waits; that is fine in a thread pool and exactly why you must not call it from `async def` code (use the async variant there).
* `rate_limit` with `block=True` would hold a request open until the window passes. For an API you almost always want the default (raise and return 429).

## Test it

Tests of cached code need two things: control over **time** (`fake_clock`, so a TTL passes instantly) and a clean slate (`cache_clear`). The block below is a complete, runnable test file for the core above.

```python
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

from python_wrap import RateLimitExceeded, memoize, rate_limit, singleflight
from python_wrap.testing import fake_clock

DB_QUERIES: list[int] = []


# Order matters: memoize outside (check the cache first), singleflight inside (only one
# miss per key goes to the database at a time).
@memoize(ttl=60, maxsize=1000)
@singleflight
def load_profile(user_id: int) -> dict[str, object]:
    DB_QUERIES.append(user_id)
    time.sleep(0.03)  # a slow query
    return {"id": user_id, "name": f"user{user_id}"}


# Async code: memoize already shares one in-flight task between concurrent callers.
@memoize(ttl=60)
async def load_feed(user_id: int) -> list[str]:
    DB_QUERIES.append(-user_id)
    await asyncio.sleep(0.02)
    return [f"post {n} for {user_id}" for n in range(3)]


@rate_limit(3, 60, key=lambda user, *args, **kwargs: user)  # 3 searches/minute/user
def search(user: str, query: str) -> list[str]:
    return [f"{query} result {n}" for n in range(2)]


def search_endpoint(user: str, query: str) -> tuple[int, dict[str, object]]:
    """What an HTTP layer does with the exceptions: map them to status codes."""
    try:
        return 200, {"results": search(user, query)}
    except RateLimitExceeded as exc:
        return 429, {"error": "slow down", "retry_after": round(exc.retry_after)}

from concurrent.futures import ThreadPoolExecutor

from python_wrap.testing import spy


def test_thundering_herd_is_one_query() -> None:
    load_profile.cache_clear()
    DB_QUERIES.clear()
    with fake_clock(), ThreadPoolExecutor(16) as pool:
        results = list(pool.map(load_profile, [900] * 16))
    assert len({id(r) for r in results}) == 1  # everyone got the same object
    assert DB_QUERIES == [900]


def test_entry_expires_after_ttl() -> None:
    DB_QUERIES.clear()
    with fake_clock() as clock:  # install it BEFORE the first call
        load_profile.cache_clear()
        load_profile(901)
        clock.advance(59)
        load_profile(901)
        assert DB_QUERIES == [901]  # still fresh
        clock.advance(2)
        load_profile(901)
        assert DB_QUERIES == [901, 901]  # expired and reloaded


def test_rate_limit_is_per_user() -> None:
    with fake_clock():
        assert [search_endpoint("tess", "q")[0] for _ in range(4)] == [200, 200, 200, 429]
        status, body = search_endpoint("tess", "q")
        assert status == 429 and body["retry_after"] > 0
        assert search_endpoint("uma", "q")[0] == 200  # someone else is not affected


def test_cache_hits_are_visible_with_a_spy() -> None:
    spied = spy(lambda user_id: {"id": user_id})
    cached = memoize(spied)
    cached(1)
    cached(1)
    cached(2)
    assert spied.call_count == 2  # the second call for user 1 never reached the function
    assert cached.cache_info().hits == 1


# pytest collects the test_ functions itself; this loop only lets the page run them directly.
for _name, _test in list(globals().items()):
    if _name.startswith("test_"):
        _test()
```

For tests of the HTTP layer, use FastAPI's `TestClient` and reset state between tests:

<!-- skip -->
```python
import pytest
from fastapi.testclient import TestClient

from myapp.main import app, load_feed, load_profile
from python_wrap.testing import disable_caching, disable_rate_limits


@pytest.fixture(autouse=True)
def clean_state():
    load_profile.cache_clear()
    load_feed.cache_clear()
    yield


def test_profile_endpoint_not_cached_in_tests():
    # Switch off every cache and limiter for this test only.
    with disable_caching(), disable_rate_limits():
        client = TestClient(app)
        assert client.get("/profile/1", headers={"x-user": "a"}).status_code == 200
```

`disable_caching()` is the right switch for a test of the endpoint's own logic; use `fake_clock` and `cache_clear` (as above) for tests that are *about* the caching.
