"""Async-compatible Redis backends: ThreadedStore and AsyncRedisWindow, with fakes."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any

import pytest

from python_wrap import RateLimiter, RedisStore
from python_wrap.async_cache import AsyncCacheStore, ThreadedStore, amemoize
from python_wrap.errors import RateLimitExceeded
from python_wrap.quotas import AsyncRedisWindow, RedisWindow, shared_rate_limit
from python_wrap.testing import disable_rate_limits

# ------------------------------------------------------------------------ fakes


class BlockingRedis:
    """A synchronous redis.Redis stand-in that records the calling thread."""

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.threads: set[int] = set()

    def _seen(self) -> None:
        self.threads.add(threading.get_ident())

    def get(self, key: str) -> bytes | None:
        self._seen()
        return self.data.get(key)

    def set(self, key: str, value: bytes, **options: Any) -> None:
        self._seen()
        self.data[key] = value

    def delete(self, *keys: str) -> int:
        self._seen()
        return sum(self.data.pop(key, None) is not None for key in keys)

    def scan_iter(self, match: str, count: int) -> list[str]:
        self._seen()
        prefix = match.rstrip("*").replace("\\", "")
        return [key for key in list(self.data) if key.startswith(prefix)]


class FakeAsyncRedis:
    """A redis.asyncio stand-in: admits ``capacity`` calls, then reports a wait."""

    def __init__(self, capacity: int, wait_ms: int = 250) -> None:
        self.capacity, self.wait_ms = capacity, wait_ms
        self.evals: list[tuple[Any, ...]] = []
        self.keys = {"w:a", "w:b", "other"}
        self.deleted: list[str] = []

    async def eval(self, *args: Any) -> int:
        await asyncio.sleep(0)
        self.evals.append(args)
        return 0 if len(self.evals) <= self.capacity else self.wait_ms

    async def scan_iter(self, match: str, count: int) -> Any:
        prefix = match.rstrip("*")
        for key in sorted(self.keys):
            if key.startswith(prefix):
                yield key

    async def delete(self, key: str) -> int:
        self.deleted.append(key)
        return 1


# ------------------------------------------------------------------ ThreadedStore


def test_threaded_store_runs_redis_store_off_the_loop() -> None:
    client = BlockingRedis()
    store = ThreadedStore(RedisStore(client, "users:"))
    assert isinstance(store, AsyncCacheStore)
    calls: list[int] = []

    @amemoize(store=store, ttl=60)
    async def lookup(user_id: int) -> str:
        calls.append(user_id)
        return f"user-{user_id}"

    async def main() -> int:
        loop_thread = threading.get_ident()
        assert await lookup(1) == "user-1"
        assert await lookup(1) == "user-1"
        assert calls == [1]
        assert loop_thread not in client.threads
        assert await store.delete(("never",)) is False
        await lookup.cache_clear()  # type: ignore[attr-defined]
        return len(client.data)

    assert asyncio.run(main()) == 0
    assert store.size() is None  # RedisStore does not count entries


def test_threaded_store_with_executor_and_len_fallback() -> None:
    class DictStore:
        def __init__(self) -> None:
            self.data: dict[Any, Any] = {}

        def get(self, key: Any) -> Any:
            return self.data.get(key)

        def set(self, key: Any, entry: Any, ttl: float | None) -> None:
            self.data[key] = entry

        def delete(self, key: Any) -> bool:
            return self.data.pop(key, None) is not None

        def clear(self) -> None:
            self.data.clear()

        def __len__(self) -> int:
            return len(self.data)

    with concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix="cache-io") as pool:
        store = ThreadedStore(DictStore(), executor=pool)

        async def main() -> None:
            await store.set("k", ("v", None), None)
            assert await store.get("k") == ("v", None)
            assert store.size() == 1
            assert await store.delete("k") is True
            await store.set("x", 1, None)
            await store.clear()
            assert store.size() == 0

        asyncio.run(main())

    class NoLen:
        def get(self, key: Any) -> None: ...
        def set(self, key: Any, entry: Any, ttl: Any) -> None: ...
        def delete(self, key: Any) -> bool:
            return False

        def clear(self) -> None: ...
        def size(self) -> int:
            return 3

    assert ThreadedStore(NoLen()).size() == 3

    class Bare:
        def get(self, key: Any) -> None: ...
        def set(self, key: Any, entry: Any, ttl: Any) -> None: ...
        def delete(self, key: Any) -> bool:
            return False

        def clear(self) -> None: ...

    assert ThreadedStore(Bare()).size() is None


def test_threaded_store_rejects_bad_stores() -> None:
    with pytest.raises(TypeError, match="get"):
        ThreadedStore(object())

    class AlreadyAsync:
        async def get(self, key: Any) -> None: ...
        async def set(self, key: Any, entry: Any, ttl: Any) -> None: ...
        async def delete(self, key: Any) -> bool:
            return False

        async def clear(self) -> None: ...

    with pytest.raises(TypeError, match="already asynchronous"):
        ThreadedStore(AlreadyAsync())


# --------------------------------------------------------------- AsyncRedisWindow


def test_async_window_matches_sync_window_arguments() -> None:
    fake = FakeAsyncRedis(capacity=10)
    window = AsyncRedisWindow(fake, "w", calls=3, period=1.5)
    assert asyncio.run(window.acquire("k")) == 0.0
    script, nkeys, key, calls, period_ms, member = fake.evals[0]
    sync_args = RedisWindow(object(), "w", calls=3, period=1.5)._eval_args("k")
    assert (script, nkeys, key, calls, period_ms) == sync_args[:5]
    assert isinstance(member, str) and len(member) == 32
    with pytest.raises(ValueError):
        AsyncRedisWindow(fake, "", calls=1, period=1)


def test_async_window_clear_deletes_only_its_keys() -> None:
    fake = FakeAsyncRedis(capacity=1)
    asyncio.run(AsyncRedisWindow(fake, "w", calls=1, period=1).clear())
    assert fake.deleted == ["w:a", "w:b"]


def test_shared_rate_limit_awaits_async_window() -> None:
    fake = FakeAsyncRedis(capacity=2)
    window = AsyncRedisWindow(fake, "api", calls=2, period=1)

    @shared_rate_limit(window)
    async def call() -> str:
        return "ok"

    async def main() -> None:
        loop_thread = threading.get_ident()
        assert [await call(), await call()] == ["ok", "ok"]
        with pytest.raises(RateLimitExceeded) as info:
            await call()
        assert info.value.retry_after == 0.25
        assert threading.get_ident() == loop_thread

    asyncio.run(main())

    with pytest.raises(TypeError, match="synchronous"):

        @shared_rate_limit(window)
        def sync_call() -> None: ...


def test_shared_rate_limit_async_window_blocks_and_respects_overrides() -> None:
    class OneWait(FakeAsyncRedis):
        async def eval(self, *args: Any) -> int:
            self.evals.append(args)
            return 1 if len(self.evals) == 2 else 0  # the second call waits once

    fake = OneWait(capacity=0)

    @shared_rate_limit(AsyncRedisWindow(fake, "api", calls=1, period=1), block=True)
    async def call() -> str:
        return "ok"

    async def main() -> None:
        assert await call() == "ok"
        assert await call() == "ok"

    asyncio.run(main())
    assert len(fake.evals) == 3
    with disable_rate_limits():
        assert asyncio.run(call()) == "ok"
    assert len(fake.evals) == 3


def test_rate_limiter_with_async_backend() -> None:
    fake = FakeAsyncRedis(capacity=1, wait_ms=500)
    window = AsyncRedisWindow(fake, "rl", calls=1, period=1)
    limiter = RateLimiter(backend=window, name="rl")

    @limiter
    async def call() -> int:
        return 1

    async def main() -> None:
        assert await call() == 1
        with pytest.raises(RateLimitExceeded):
            await call()
        assert await limiter.acquire_async(block=False) is False

    asyncio.run(main())
    with pytest.raises(TypeError, match="async backend"):
        limiter.try_acquire()
    with pytest.raises(TypeError, match="async backend"):
        limiter.reset()
    with pytest.raises(TypeError, match="synchronous"):

        @limiter
        def sync_call() -> None: ...


def test_rate_limiter_async_backend_fast_mode_admits() -> None:
    from python_wrap.testing import fast_mode

    fake = FakeAsyncRedis(capacity=1, wait_ms=500)
    limiter = RateLimiter(backend=AsyncRedisWindow(fake, "rl", calls=1, period=1), block=True)

    async def main() -> None:
        assert await limiter.acquire_async()
        assert await limiter.acquire_async()

    with fast_mode():
        asyncio.run(main())
