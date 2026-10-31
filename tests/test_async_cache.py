"""Tests for python_wrap.async_cache."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from python_wrap import events
from python_wrap.async_cache import (
    AsyncCacheStore,
    AsyncMemoryStore,
    AsyncRedisStore,
    amemoize,
    json_codec,
)
from python_wrap.testing import FakeClock, disable_caching


class FakeAsyncRedis:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.expiries: dict[str, int] = {}
        self.fail = False
        self.unlinked: list[tuple[Any, ...]] = []

    def _check(self) -> None:
        if self.fail:
            raise ConnectionError("down")

    async def get(self, key: str) -> bytes | None:
        self._check()
        return self.data.get(key)

    async def set(self, key: str, value: bytes, px: int | None = None) -> None:
        self._check()
        self.data[key] = value
        if px is not None:
            self.expiries[key] = px

    async def delete(self, *keys: str) -> int:
        self._check()
        return sum(self.data.pop(k, None) is not None for k in keys)

    async def unlink(self, *keys: str) -> int:
        self.unlinked.append(keys)
        return await self.delete(*keys)

    async def scan_iter(self, match: str, count: int = 10) -> AsyncIterator[str]:
        self._check()
        prefix = match.rstrip("*").replace("\\", "")
        for k in list(self.data):
            if k.startswith(prefix):
                yield k


async def test_hits_and_misses() -> None:
    calls = 0

    @amemoize()
    async def f(a: int, b: int = 2) -> int:
        nonlocal calls
        calls += 1
        return a + b

    assert await f(1) == 3
    assert await f(1, 2) == 3
    assert await f(1, b=2) == 3
    assert await f(2) == 4
    assert calls == 2
    info = f.cache_info()  # type: ignore[attr-defined]
    assert (info.hits, info.misses, info.currsize) == (2, 2, 2)
    assert await f.cache_delete(1) is True  # type: ignore[attr-defined]
    assert await f.cache_delete(1) is False  # type: ignore[attr-defined]
    await f(1)
    assert calls == 3
    await f.cache_clear()  # type: ignore[attr-defined]
    assert f.cache_info().hits == 0  # type: ignore[attr-defined]


async def test_ttl_with_fake_clock(fake_clock: FakeClock) -> None:
    calls = 0

    @amemoize(ttl=10)
    async def f() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await f() == 1
    fake_clock.advance(9)
    assert await f() == 1
    fake_clock.advance(2)
    assert await f() == 2


async def test_entry_deadline_checked_even_if_store_keeps_it(fake_clock: FakeClock) -> None:
    class Forever(AsyncMemoryStore):
        async def set(self, key: Any, entry: Any, ttl: float | None, /) -> None:
            await super().set(key, entry, None)

    calls = 0

    @amemoize(ttl=5, store=Forever())
    async def f() -> int:
        nonlocal calls
        calls += 1
        return calls

    await f()
    fake_clock.advance(6)
    assert await f() == 2


async def test_singleflight_shares_one_execution() -> None:
    calls = 0

    @amemoize()
    async def f() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return 7

    assert await asyncio.gather(*(f() for _ in range(10))) == [7] * 10
    assert calls == 1


async def test_singleflight_disabled_runs_each() -> None:
    calls = 0

    @amemoize(singleflight=False)
    async def f() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return 1

    await asyncio.gather(*(f() for _ in range(3)))
    assert calls == 3


async def test_exception_not_cached_and_reaches_all_waiters() -> None:
    calls = 0

    @amemoize()
    async def f() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        if calls == 1:
            raise RuntimeError("boom")
        return 5

    results = await asyncio.gather(*(f() for _ in range(4)), return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)
    assert calls == 1
    assert await f() == 5


async def test_waiter_cancellation_does_not_cancel_shared_computation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    @amemoize()
    async def f() -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "done"

    first = asyncio.create_task(f())
    await started.wait()
    second = asyncio.create_task(f())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == "done"
    assert await f() == "done"
    assert calls == 1


async def test_cache_none_flag() -> None:
    calls = 0

    @amemoize(cache_none=False)
    async def f() -> None:
        nonlocal calls
        calls += 1

    await f()
    await f()
    assert calls == 2


async def test_cache_none_default_caches() -> None:
    calls = 0

    @amemoize()
    async def f() -> None:
        nonlocal calls
        calls += 1

    await f()
    await f()
    assert calls == 1


async def test_copy_result_isolates() -> None:
    @amemoize(copy_result=True)
    async def f() -> list[int]:
        return [1]

    a = await f()
    a.append(2)
    assert await f() == [1]


async def test_custom_key() -> None:
    calls = 0

    @amemoize(key=lambda a, b: a)
    async def f(a: int, b: int) -> int:
        nonlocal calls
        calls += 1
        return b

    assert await f(1, 10) == 10
    assert await f(1, 20) == 10
    assert calls == 1


async def test_disable_caching() -> None:
    calls = 0

    @amemoize()
    async def f() -> int:
        nonlocal calls
        calls += 1
        return 1

    with disable_caching():
        await f()
        await f()
    assert calls == 2
    await f()
    await f()
    assert calls == 3


async def test_sync_function_rejected() -> None:
    with pytest.raises(TypeError):

        @amemoize()
        def f() -> int:  # type: ignore[type-var]
            return 1


def test_bad_options() -> None:
    with pytest.raises(ValueError):
        amemoize(ttl=0)
    with pytest.raises(TypeError):
        amemoize(singleflight="yes")  # type: ignore[arg-type]


async def test_events_emitted() -> None:
    seen: list[str] = []

    @amemoize()
    async def f() -> int:
        return 1

    with events.listening(lambda e: seen.append(e.name)):
        await f()
        await f()
    assert seen == ["cache.miss", "cache.hit"]


async def test_memory_store_lru_and_protocol() -> None:
    store = AsyncMemoryStore(maxsize=2)
    assert isinstance(store, AsyncCacheStore)
    await store.set("a", 1, None)
    await store.set("b", 2, None)
    assert await store.get("a") == 1
    await store.set("c", 3, None)
    assert await store.get("b") is None
    assert await store.get("a") == 1
    await store.clear()
    assert len(store) == 0
    with pytest.raises(ValueError):
        AsyncMemoryStore(maxsize=0)


async def test_memory_store_ttl_on_read(fake_clock: FakeClock) -> None:
    store = AsyncMemoryStore()
    await store.set("a", 1, 5)
    fake_clock.advance(6)
    assert await store.get("a") is None
    assert len(store) == 0


async def test_redis_store_roundtrip_and_ttl() -> None:
    client = FakeAsyncRedis()
    store = AsyncRedisStore(client, "p:")
    assert isinstance(store, AsyncCacheStore)
    assert await store.get(("k",)) is None
    await store.set(("k",), ("v", None), 1.5)
    assert await store.get(("k",)) == ("v", None)
    (stored_key,) = client.data
    assert stored_key.startswith("p:") and "k" not in stored_key[2:]
    assert client.expiries[stored_key] == 1500
    assert await store.delete(("k",)) is True
    assert await store.delete(("k",)) is False
    await store.set(("k",), 1, 0)
    assert client.data == {}


async def test_redis_store_key_format_and_empty_prefix() -> None:
    client = FakeAsyncRedis()
    store = AsyncRedisStore(client, "p:", key_format=lambda k: f"id-{k}")
    await store.set(1, "x", None)
    assert list(client.data) == ["p:id-1"]
    with pytest.raises(ValueError):
        AsyncRedisStore(client, "")


async def test_redis_store_clear_only_prefix() -> None:
    client = FakeAsyncRedis()
    mine = AsyncRedisStore(client, "mine:")
    other = AsyncRedisStore(client, "other:")
    for i in range(450):
        await mine.set(i, i, None)
    await other.set("x", 1, None)
    await mine.clear()
    assert len(client.data) == 1
    assert await other.get("x") == 1
    assert [len(b) for b in client.unlinked] == [200, 200, 50]


async def test_redis_store_fail_open_true(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeAsyncRedis()
    client.fail = True
    store = AsyncRedisStore(client, "p:")
    with caplog.at_level(logging.WARNING, logger="python_wrap"):
        assert await store.get("k") is None
        await store.set("k", 1, None)
        assert await store.delete("k") is False
        await store.clear()
    assert "AsyncRedisStore operation failed" in caplog.text


async def test_redis_store_fail_open_false() -> None:
    client = FakeAsyncRedis()
    client.fail = True
    store = AsyncRedisStore(client, "p:", fail_open=False)
    with pytest.raises(ConnectionError):
        await store.get("k")
    with pytest.raises(ConnectionError):
        await store.set("k", 1, None)
    with pytest.raises(ConnectionError):
        await store.clear()


async def test_json_codec_roundtrip_with_amemoize(fake_clock: FakeClock) -> None:
    dumps, loads = json_codec()
    assert loads(dumps({"a": [1, None]})) == {"a": [1, None]}
    client = FakeAsyncRedis()
    calls = 0

    @amemoize(ttl=10, store=AsyncRedisStore(client, "j:", dumps=dumps, loads=loads))
    async def f(n: int) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"n": n}

    assert await f(1) == {"n": 1}
    assert await f(1) == {"n": 1}
    assert calls == 1
    assert next(iter(client.data.values())).startswith(b"[")
    fake_clock.advance(11)
    await f(1)
    assert calls == 2


async def test_amemoize_with_pickle_redis_store() -> None:
    client = FakeAsyncRedis()
    calls = 0

    @amemoize(ttl=60, store=AsyncRedisStore(client, "pk:"))
    async def f(n: int) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        return (n, n)

    assert await f(2) == (2, 2)
    assert await f(2) == (2, 2)
    assert calls == 1
    assert f.cache_info().currsize is None  # type: ignore[attr-defined]
