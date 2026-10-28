"""memoize extras: stale-while-revalidate, pluggable stores and per-instance caches."""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import datetime
import gc
import logging
import pickle
import threading
import weakref
from collections.abc import Hashable, Iterator
from typing import Any

import pytest

from conftest import FakeClock
from python_wrap import _core
from python_wrap.caching import CacheInfo, CacheStore, memoize
from python_wrap.testing import disable_caching


@pytest.fixture
def events() -> Iterator[list[_core.Event]]:
    seen: list[_core.Event] = []
    _core.listeners.append(seen.append)
    try:
        yield seen
    finally:
        _core.listeners.remove(seen.append)


def names(events: list[_core.Event], prefix: str = "cache.") -> list[str]:
    return [e.name for e in events if e.name.startswith(prefix)]


def join_refreshes() -> None:
    for t in threading.enumerate():
        if t.name.startswith("python_wrap-refresh-"):
            t.join(2)


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


class DictStore:
    def __init__(self) -> None:
        self.data: dict[Hashable, Any] = {}
        self.ttls: list[float | None] = []

    def get(self, key: Hashable) -> Any:
        return self.data.get(key)

    def set(self, key: Hashable, entry: Any, ttl: float | None) -> None:
        self.ttls.append(ttl)
        self.data[key] = entry

    def delete(self, key: Hashable) -> bool:
        return self.data.pop(key, None) is not None

    def clear(self) -> None:
        self.data.clear()

    def __len__(self) -> int:
        return len(self.data)


class PickleStore(DictStore):
    """Serialises keys and entries, as a Redis adapter would."""

    def get(self, key: Hashable) -> Any:
        raw = self.data.get(pickle.dumps(key))
        return None if raw is None else pickle.loads(raw)

    def set(self, key: Hashable, entry: Any, ttl: float | None) -> None:
        self.data[pickle.dumps(key)] = pickle.dumps(entry)

    def delete(self, key: Hashable) -> bool:
        return self.data.pop(pickle.dumps(key), None) is not None


# ------------------------------------------------------------ stale-while-revalidate


def test_stale_value_is_served_and_refreshed_in_background(
    fake_clock: FakeClock, events: list[_core.Event]
) -> None:
    version = [1]

    @memoize(ttl=10, stale_ttl=30)
    def load(key: str) -> int:
        return version[0]

    assert load("a") == 1
    version[0] = 2
    fake_clock.advance(5)
    assert load("a") == 1  # fresh
    fake_clock.advance(10)
    assert load("a") == 1  # stale: served immediately, refresh started
    join_refreshes()
    assert load("a") == 2
    assert names(events) == ["cache.miss", "cache.hit", "cache.hit", "cache.stale", "cache.hit"]
    assert load.cache_info().hits == 3
    assert load.cache_parameters()["stale_ttl"] == 30.0


def test_past_the_stale_window_is_a_plain_miss(fake_clock: FakeClock) -> None:
    calls = []

    @memoize(ttl=datetime.timedelta(seconds=10), stale_ttl=datetime.timedelta(seconds=5))
    def load() -> int:
        calls.append(1)
        return len(calls)

    assert load() == 1
    fake_clock.advance(16)
    assert load() == 2  # computed in the caller, not in the background
    assert len(calls) == 2


def test_one_refresh_thread_per_key(fake_clock: FakeClock) -> None:
    release = threading.Event()
    calls = []

    @memoize(ttl=1, stale_ttl=100)
    def load(key: int) -> int:
        calls.append(key)
        if len(calls) > 2:
            release.wait(2)
        return len(calls)

    load(1)
    load(2)
    fake_clock.advance(2)
    results = [load(1) for _ in range(5)] + [load(2)]
    assert results == [1, 1, 1, 1, 1, 2]
    release.set()
    join_refreshes()
    assert sorted(calls) == [1, 1, 2, 2]  # one refresh for each key


def test_refresh_thread_sees_the_callers_context(fake_clock: FakeClock) -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("var", default="unset")
    seen = []

    @memoize(ttl=1, stale_ttl=10)
    def load() -> str:
        seen.append(var.get())
        return var.get()

    load()
    fake_clock.advance(2)
    var.set("request-42")
    load()
    join_refreshes()
    assert seen == ["unset", "request-42"]


def test_failed_refresh_keeps_the_stale_value(
    fake_clock: FakeClock, events: list[_core.Event], caplog: pytest.LogCaptureFixture
) -> None:
    fail = [False]
    calls = []

    @memoize(ttl=1, stale_ttl=10)
    def load() -> int:
        calls.append(1)
        if fail[0]:
            raise ConnectionError("down")
        return len(calls)

    assert load() == 1
    fake_clock.advance(2)
    fail[0] = True
    with caplog.at_level(logging.WARNING, logger="python_wrap"):
        assert load() == 1
        join_refreshes()
    assert "background refresh" in caplog.text
    failed = [e for e in events if e.name == "cache.refresh_failed"]
    assert len(failed) == 1 and isinstance(failed[0].fields["exception"], ConnectionError)
    fail[0] = False
    assert load() == 1  # still stale: the next call retries
    join_refreshes()
    assert load() == 3
    assert len(calls) == 3


def test_cached_exceptions_are_not_served_stale(fake_clock: FakeClock) -> None:
    calls = []

    @memoize(ttl=1, stale_ttl=10, cache_exceptions=KeyError)
    def load() -> int:
        calls.append(1)
        raise KeyError("x")

    for _ in range(2):
        with pytest.raises(KeyError):
            load()
    fake_clock.advance(2)
    with pytest.raises(KeyError):
        load()
    assert len(calls) == 2


def test_stale_with_callable_ttl(fake_clock: FakeClock) -> None:
    version = [1]

    @memoize(ttl=lambda value: None if value == 0 else 5, stale_ttl=5)
    def load(n: int) -> int:
        return n * version[0]

    load(0)
    load(1)
    version[0] = 10
    fake_clock.advance(6)
    assert load(0) == 0  # ttl None: never stale
    assert load(1) == 1  # stale
    join_refreshes()
    assert load(1) == 10


def test_stale_ttl_validation() -> None:
    with pytest.raises(ValueError, match="requires ttl"):
        memoize(stale_ttl=5)
    with pytest.raises(ValueError):
        memoize(ttl=5, stale_ttl=0)


def test_stale_respects_disable_caching(fake_clock: FakeClock) -> None:
    calls = []

    @memoize(ttl=1, stale_ttl=10)
    def load() -> int:
        calls.append(1)
        return len(calls)

    load()
    fake_clock.advance(2)
    with disable_caching():
        assert load() == 2
        assert load() == 3
    join_refreshes()
    assert load() == 1  # the stale entry is untouched; its refresh starts now
    join_refreshes()
    assert load() == 4


def test_cache_clear_discards_a_running_refresh(fake_clock: FakeClock) -> None:
    started, release = threading.Event(), threading.Event()
    calls = []

    @memoize(ttl=1, stale_ttl=10)
    def load() -> int:
        calls.append(1)
        if len(calls) == 2:
            started.set()
            release.wait(2)
        return len(calls)

    load()
    fake_clock.advance(2)
    load()
    assert started.wait(2)
    load.cache_clear()
    release.set()
    join_refreshes()
    assert load.cache_info().currsize == 0


def test_async_stale_refresh_runs_once(fake_clock: FakeClock, events: list[_core.Event]) -> None:
    calls = []

    @memoize(ttl=1, stale_ttl=10)
    async def load(key: str) -> int:
        calls.append(key)
        await asyncio.sleep(0)
        return len(calls)

    async def main() -> None:
        assert await load("a") == 1
        fake_clock.advance(2)
        assert await asyncio.gather(load("a"), load("a"), load(key="a")) == [1, 1, 1]
        await settle()
        assert await load("a") == 2

    asyncio.run(main())
    assert calls == ["a", "a"]
    assert names(events).count("cache.stale") == 1


def test_async_failed_refresh_keeps_stale(fake_clock: FakeClock, events: list[_core.Event]) -> None:
    fail = [False]

    @memoize(ttl=1, stale_ttl=10)
    async def load() -> int:
        if fail[0]:
            raise ConnectionError("down")
        return 1

    async def main() -> None:
        await load()
        fake_clock.advance(2)
        fail[0] = True
        assert await load() == 1
        await settle()
        assert await load() == 1
        await settle()

    asyncio.run(main())
    assert names(events).count("cache.refresh_failed") == 2


# ------------------------------------------------------------------- custom stores


def test_custom_store_round_trip(fake_clock: FakeClock, events: list[_core.Event]) -> None:
    store = DictStore()
    calls = []

    @memoize(store=store, ttl=10, stale_ttl=5)
    def double(n: int) -> int:
        calls.append(n)
        return n * 2

    assert isinstance(store, CacheStore)
    assert double(2) == double(n=2) == 4
    assert calls == [2]
    assert store.ttls == [15.0]  # the store may discard the entry after ttl + stale_ttl
    assert double.cache_info() == CacheInfo(hits=1, misses=1, maxsize=None, currsize=1)
    assert double.cache_contains(2) and double.cache_get(2) == 4
    assert double.cache_delete(2) is True and double.cache_delete(2) is False
    double(3)
    fake_clock.advance(20)
    assert not double.cache_contains(3)
    double(3)
    assert names(events)[-2:] == ["cache.evicted", "cache.miss"]
    double.cache_clear()
    assert len(store) == 0 and double.cache_info() == (0, 0, None, 0)
    assert double.cache_parameters()["store"] is store


def test_store_entries_survive_serialisation(fake_clock: FakeClock) -> None:
    store = PickleStore()
    calls = []

    @memoize(store=store, ttl=10, cache_exceptions=LookupError)
    def find(items: list[int], missing: bool = False) -> int:
        calls.append(1)
        if missing:
            raise LookupError("nope")
        return sum(items)

    assert find([1, 2]) == find(items=[1, 2]) == 3
    for _ in range(2):
        with pytest.raises(LookupError):
            find([1], missing=True)
    assert len(calls) == 2
    assert all(isinstance(k, bytes) for k in store.data)
    fake_clock.advance(11)
    find([1, 2])
    assert len(calls) == 3


def test_custom_store_async() -> None:
    store = DictStore()
    calls = []

    @memoize(store=store)
    async def load(n: int) -> int:
        calls.append(n)
        await asyncio.sleep(0)
        return n

    async def main() -> None:
        assert await asyncio.gather(load(1), load(1)) == [1, 1]
        assert await load(1) == 1

    asyncio.run(main())
    assert calls == [1]
    assert load.cache_info().hits == 2


def test_custom_store_unhashable_argument_raises() -> None:
    @memoize(store=DictStore())
    def f(x: Any) -> Any:
        return x

    with pytest.raises(TypeError, match="not hashable"):
        f(object.__new__(type("U", (), {"__hash__": None})))


def test_custom_store_threads() -> None:
    store = DictStore()

    @memoize(store=store)
    def f(n: int) -> int:
        return n * n

    def worker() -> None:
        for i in range(200):
            assert f(i % 20) == (i % 20) ** 2

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    info = f.cache_info()
    assert info.hits + info.misses == 1600 and info.currsize == 20


def test_store_option_errors() -> None:
    with pytest.raises(TypeError, match="maxsize"):
        memoize(store=DictStore(), maxsize=3)
    with pytest.raises(TypeError, match="per_instance"):
        memoize(store=DictStore(), per_instance=True)
    with pytest.raises(TypeError, match="CacheStore"):
        memoize(store=object())  # type: ignore[arg-type]


# ----------------------------------------------------------------- per_instance


@dataclasses.dataclass
class Point:  # eq=True: instances are unhashable
    x: int

    @memoize(per_instance=True, maxsize=2)
    def scaled(self, k: int) -> int:
        calls.append((self.x, k))
        return self.x * k


calls: list[tuple[int, int]] = []


def test_per_instance_unhashable_identity_and_gc() -> None:
    calls.clear()
    Point.scaled.cache_clear()
    a, b = Point(2), Point(2)  # equal but distinct: separate caches
    assert a.scaled(3) == a.scaled(k=3) == b.scaled(3) == 6
    assert calls == [(2, 3), (2, 3)]
    assert Point.scaled.cache_info() == CacheInfo(hits=1, misses=2, maxsize=2, currsize=2)
    ref = weakref.ref(a)
    del a
    gc.collect()
    assert ref() is None
    assert Point.scaled.cache_info().currsize == 1


def test_per_instance_maxsize_and_controls() -> None:
    calls.clear()
    Point.scaled.cache_clear()
    p = Point(1)
    for k in (1, 2, 3):
        p.scaled(k)
    assert Point.scaled.cache_info().currsize == 2  # LRU per instance
    assert not p.scaled.cache_contains(p, 1)
    assert p.scaled.cache_get(p, 3) == 3
    assert Point.scaled.cache_delete(p, 3) is True
    other = Point(9)
    assert not Point.scaled.cache_contains(other, 3)
    assert Point.scaled.cache_delete(other, 3) is False
    with pytest.raises(KeyError):
        Point.scaled.cache_get(other, 3)
    Point.scaled.cache_clear()
    assert Point.scaled.cache_info() == (0, 0, 2, 0)
    p.scaled(2)
    assert calls[-1] == (1, 2)


def test_per_instance_needs_weakref() -> None:
    class Slotted:
        __slots__ = ("v",)

        @memoize(per_instance=True)
        def get(self) -> int:
            return 1

    class SlottedWeak:
        __slots__ = ("__weakref__",)

        @memoize(per_instance=True)
        def get(self) -> int:
            return 2

    with pytest.raises(TypeError, match="__weakref__"):
        Slotted().get()
    assert SlottedWeak().get() == 2


def test_per_instance_argument_errors() -> None:
    with pytest.raises(TypeError, match="method"):

        @memoize(per_instance=True)
        def nothing() -> None: ...

    @memoize(per_instance=True)
    def anything(*args: Any) -> int:
        return len(args)

    with pytest.raises(TypeError, match="first positional"):
        anything()
    assert anything.cache_delete() is False


def test_per_instance_ttl_key_typed_and_classmethod(fake_clock: FakeClock) -> None:
    seen = []

    class Svc:
        @memoize(per_instance=True, ttl=5, typed=True)
        def get(self, n: Any) -> str:
            return f"{n!r}"

        @memoize(per_instance=True, key=lambda self, n, verbose=False: n)
        def label(self, n: int, verbose: bool = False) -> int:
            seen.append(n)
            return n

        @classmethod
        @memoize(per_instance=True)
        def make(cls, n: int) -> int:
            return n

    s = Svc()
    assert s.get(1) == "1" and s.get(1.0) == "1.0"
    fake_clock.advance(6)
    assert Svc.get.cache_info().currsize == 0  # swept
    s.label(1)
    s.label(1, verbose=True)
    assert seen == [1]
    assert Svc.make(3) == Svc.make(3) == 3


def test_per_instance_without_signature() -> None:
    class NoSignature:
        __signature__ = "not a signature"

        def __call__(self, *args: Any, **kwargs: Any) -> int:
            return len(args) + len(kwargs)

    f = memoize(per_instance=True)(NoSignature())

    class Owner:
        pass

    o = Owner()
    assert f(o, 1, b=2) == 3
    assert f.cache_contains(o, 1, b=2)


def test_per_instance_async_and_threads() -> None:
    runs = []

    class Repo:
        @memoize(per_instance=True)
        async def load(self, key: int) -> int:
            runs.append(key)
            await asyncio.sleep(0)
            return key

        @memoize(per_instance=True)
        def sync_load(self, key: int) -> int:
            return key * 2

    r1, r2 = Repo(), Repo()

    async def main() -> None:
        assert await asyncio.gather(r1.load(1), r1.load(1), r2.load(1)) == [1, 1, 1]
        assert await r1.load(1) == 1

    asyncio.run(main())
    assert runs == [1, 1]

    def worker() -> None:
        for i in range(100):
            assert r1.sync_load(i % 10) == (i % 10) * 2

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert Repo.sync_load.cache_info().currsize == 10


def test_per_instance_with_stale_refresh(fake_clock: FakeClock) -> None:
    class Feed:
        def __init__(self) -> None:
            self.version = 1

        @memoize(per_instance=True, ttl=1, stale_ttl=10)
        def latest(self) -> int:
            return self.version

        @memoize(per_instance=True, ttl=1, stale_ttl=10)
        async def alatest(self) -> int:
            return self.version

    feed = Feed()
    assert feed.latest() == 1
    feed.version = 2
    fake_clock.advance(2)
    assert feed.latest() == 1
    join_refreshes()
    assert feed.latest() == 2

    async def main() -> None:
        assert await feed.alatest() == 2
        feed.version = 3
        fake_clock.advance(2)
        assert await feed.alatest() == 2
        await settle()
        assert await feed.alatest() == 3

    asyncio.run(main())


def test_per_instance_disable_caching() -> None:
    n = []

    class C:
        @memoize(per_instance=True)
        def f(self) -> int:
            n.append(1)
            return len(n)

    c = C()
    with disable_caching():
        assert c.f() == 1 and c.f() == 2
    assert C.f.cache_info().currsize == 0


def test_refresh_thread_start_failure_keeps_the_stale_value(
    fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch, events: list[_core.Event]
) -> None:
    @memoize(ttl=1, stale_ttl=10)
    def load() -> int:
        return 1

    load()
    fake_clock.advance(2)

    def boom(self: threading.Thread) -> None:
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as m:
        m.setattr(threading.Thread, "start", boom)
        assert load() == 1
    assert names(events)[-1] == "cache.refresh_failed"
    assert load() == 1  # the key is not stuck as "refreshing"
    assert names(events)[-1] == "cache.stale"
    join_refreshes()
