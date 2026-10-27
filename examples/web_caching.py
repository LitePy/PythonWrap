"""The caching and rate limiting a web endpoint needs, with no web framework.

Three decorators protect a slow backend: ``memoize(ttl=...)`` caches an expensive lookup,
``singleflight`` stops a crowd of simultaneous cache misses from all hitting the database
(the thundering herd), and ``rate_limit(key=...)`` gives every user their own request
budget. The handlers below are plain functions; docs/cookbook/fastapi.md shows how to
mount the same functions in FastAPI.

Run: python examples/web_caching.py
"""

from __future__ import annotations

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


# --- demo ---


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


if __name__ == "__main__":
    main()
