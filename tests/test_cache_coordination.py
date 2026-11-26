"""Cross-process stampede leases, tag invalidation and ttl jitter for memoize/amemoize."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from python_wrap import ainvalidate_tags, amemoize, invalidate_tags, memoize
from python_wrap.async_cache import AsyncMemoryStore
from python_wrap.coordination import LocalLease
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
