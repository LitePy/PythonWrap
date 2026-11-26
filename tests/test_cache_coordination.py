"""Cross-process stampede leases, tag invalidation and ttl jitter for memoize/amemoize."""

from __future__ import annotations

import asyncio
import gc
import json
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from python_wrap import ainvalidate_tags, amemoize, invalidate_tags, memoize
from python_wrap.async_cache import AsyncMemoryStore, AsyncRedisStore
from python_wrap.coordination import LocalLease
from python_wrap.stores import RedisStore
from python_wrap.testing import fake_clock


class DictStore:
    """A thread-safe shared CacheStore stand-in (what RedisStore is across processes)."""

    def __init__(self) -> None:
        self.data: dict[Any, Any] = {}
        self.lock = threading.Lock()
        self.ttls: list[float | None] = []

    def get(self, key: Any) -> Any:
        with self.lock:
            return self.data.get(key)

    def set(self, key: Any, entry: Any, ttl: float | None) -> None:
        with self.lock:
            self.data[key] = entry
            self.ttls.append(ttl)

    def delete(self, key: Any) -> bool:
        with self.lock:
            return self.data.pop(key, None) is not None

    def clear(self) -> None:
        with self.lock:
            self.data.clear()

    def __len__(self) -> int:
        return len(self.data)


class SyncRedis:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.set_calls = 0
        self.closed = False

    def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    def set(self, key: str, value: bytes, **_: Any) -> bool:
        if self.closed:
            raise RuntimeError("client is closed")
        self.set_calls += 1
        self.data[key] = value
        return True

    def delete(self, *keys: str) -> int:
        return sum(self.data.pop(key, None) is not None for key in keys)

    def scan_iter(self, **_: Any) -> Any:
        return iter(self.data)

    def close(self) -> None:
        self.closed = True


class AsyncRedis:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.set_calls = 0
        self.closed = False

    async def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    async def set(self, key: str, value: bytes, **_: Any) -> bool:
        if self.closed:
            raise RuntimeError("client is closed")
        self.set_calls += 1
        self.data[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        return sum(self.data.pop(key, None) is not None for key in keys)

    def close(self) -> None:
        self.closed = True


class SlottedRedis:
    __slots__ = ("closed", "data", "set_calls")

    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.set_calls = 0
        self.closed = False

    def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    def set(self, key: str, value: bytes, **_: Any) -> bool:
        if self.closed:
            raise RuntimeError("client is closed")
        self.set_calls += 1
        self.data[key] = value
        return True

    def delete(self, *keys: str) -> int:
        return sum(self.data.pop(key, None) is not None for key in keys)

    def scan_iter(self, **_: Any) -> Any:
        return iter(self.data)


def test_twenty_workers_missing_one_key_compute_once() -> None:
    store, calls = DictStore(), []
    start = threading.Barrier(20)

    def report(day: str) -> str:
        calls.append(day)
        time.sleep(0.05)
        return f"report {day}"

    # Twenty "processes": separate wrappers sharing one store and one lease backend.
    lease = LocalLease()
    workers = [memoize(store=store, ttl=60, lock=lease, lock_wait=5)(report) for _ in range(20)]

    def go(fn: Any) -> str:
        start.wait()
        return str(fn("mon"))

    with ThreadPoolExecutor(20) as pool:
        results = list(pool.map(go, workers))
    assert results == ["report mon"] * 20
    assert calls == ["mon"]


def test_lock_requires_a_store() -> None:
    with pytest.raises(ValueError, match="store"):
        memoize(lock=LocalLease())
    with pytest.raises(ValueError, match="store"):
        amemoize(lock=LocalLease())


def test_invalidate_tags_makes_tagged_entries_miss() -> None:
    calls: list[int] = []

    @memoize(ttl=60, tags=lambda customer_id: [f"customer:{customer_id}"])
    def orders(customer_id: int) -> int:
        calls.append(customer_id)
        return len(calls)

    assert orders(7) == orders(7) == 1
    assert orders(8) == 2
    invalidate_tags("customer:7")
    assert orders(7) == 3  # recomputed
    assert orders(8) == 2  # other tags untouched
    assert calls == [7, 8, 7]


def test_redis_tag_registry_is_weak_and_explicit_client_is_deduplicated() -> None:
    client = SyncRedis()
    client_ref = weakref.ref(client)
    store = RedisStore(client, "cache:", dumps=json.dumps, loads=json.loads)
    calls: list[int] = []

    @memoize(store=store, tags=lambda: ["t"])
    def value() -> int:
        calls.append(1)
        return len(calls)

    assert value() == 1
    client.set_calls = 0
    invalidate_tags("t", client=client)
    assert client.set_calls == 1
    assert value() == 2

    client.close()
    del value, store, client
    gc.collect()
    assert client_ref() is None
    invalidate_tags("after-gc")


def test_async_redis_tag_registry_is_weak_and_explicit_client_is_deduplicated() -> None:
    client = AsyncRedis()
    client_ref = weakref.ref(client)
    store = AsyncRedisStore(client, "cache:", dumps=json.dumps, loads=json.loads)
    calls: list[int] = []

    @amemoize(store=store, tags=lambda: ["t"])
    async def value() -> int:
        calls.append(1)
        return len(calls)

    async def check(cache: Any, redis: AsyncRedis) -> None:
        assert await cache() == 1
        redis.set_calls = 0
        await ainvalidate_tags("t", client=redis)
        assert redis.set_calls == 1
        assert await cache() == 2

    asyncio.run(check(value, client))
    client.close()
    del value, store, client
    gc.collect()
    assert client_ref() is None
    asyncio.run(ainvalidate_tags("after-gc"))


def test_tagged_slotted_redis_client_uses_weak_store_owner() -> None:
    client = SlottedRedis()
    store = RedisStore(client, "slotted:", dumps=json.dumps, loads=json.loads)

    @memoize(store=store, tags=lambda: ["slotted-tag"])
    def value() -> int:
        return 1

    assert value() == 1
    client.set_calls = 0
    invalidate_tags("slotted-tag")
    assert client.set_calls == 1


def test_invalidation_during_compute_leaves_entry_stale() -> None:
    calls: list[int] = []

    @memoize(store=DictStore(), ttl=60, tags=lambda: ["t"])
    def value() -> int:
        calls.append(1)
        if len(calls) == 1:
            invalidate_tags("t")  # the data changed while we were reading it
        return len(calls)

    assert value() == 1
    assert value() == 2


def test_amemoize_tags_and_stampede_lock() -> None:
    calls: list[int] = []
    store = AsyncMemoryStore()
    lease = LocalLease()

    def make() -> Any:
        @amemoize(store=store, ttl=60, lock=lease, tags=lambda n: [f"n:{n}"])
        async def square(n: int) -> int:
            calls.append(n)
            await asyncio.sleep(0.01)
            return n * n

        return square

    a, b = make(), make()  # two "processes"

    async def main() -> None:
        assert await asyncio.gather(a(3), b(3)) == [9, 9]
        assert calls == [3]
        await ainvalidate_tags("n:3")
        assert await b(3) == 9
        assert calls == [3, 3]

    asyncio.run(main())


def test_ttl_jitter_shortens_ttls_within_bounds() -> None:
    store = DictStore()

    @memoize(store=store, ttl=100, ttl_jitter=0.2)
    def f(n: int) -> int:
        return n

    with fake_clock():
        for n in range(50):
            f(n)
    ttls = [t for t in store.ttls if t is not None]
    assert all(80 <= t <= 100 for t in ttls)
    assert len(set(ttls)) > 1
    with pytest.raises(ValueError):
        memoize(ttl_jitter=1.0)
