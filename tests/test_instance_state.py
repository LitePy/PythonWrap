"""``key=`` / ``per_instance=`` for throttle, debounce and rate_limit; memoize(singleflight=)."""

from __future__ import annotations

import asyncio
import gc
import threading
import time
import weakref
from typing import Any

import pytest

from python_wrap import concurrency, debounce, memoize, rate_limit, throttle
from python_wrap.concurrency import limiter_of
from python_wrap.errors import RateLimitExceeded

# ---------------------------------------------------------------- throttle


def test_throttle_key_gives_each_key_a_window(fake_clock):
    calls: list[str] = []

    @throttle(5, key=lambda user, *_: user)
    def notify(user: str, text: str) -> str:
        calls.append(user)
        return f"{user}:{text}"

    assert notify("a", "1") == "a:1"
    assert notify("b", "1") == "b:1"  # another key: runs
    assert notify("a", "2") == "a:1"  # same key inside the window: skipped
    fake_clock.advance(5)
    assert notify("a", "3") == "a:3"
    assert calls == ["a", "b", "a"]


def test_throttle_per_instance(fake_clock):
    class Sensor:
        def __init__(self, name: str) -> None:
            self.name = name

        @throttle(60, per_instance=True)
        def read(self) -> str:
            return self.name

    a, b = Sensor("a"), Sensor("b")
    assert a.read() == "a"
    assert b.read() == "b"  # not skipped by a's window
    assert a.read() == "a"


def test_throttle_per_instance_unhashable_and_collectable(fake_clock):
    class Box:
        __hash__ = None  # type: ignore[assignment]

        @throttle(60, per_instance=True)
        def read(self) -> int:
            return 1

    box = Box()
    assert box.read() == 1
    ref = weakref.ref(box)
    del box
    gc.collect()
    assert ref() is None


def test_throttle_per_instance_and_key(fake_clock):
    class Feed:
        @throttle(10, per_instance=True, key=lambda self, topic: topic)
        def poll(self, topic: str) -> str:
            return f"{id(self)}:{topic}"

    f1, f2 = Feed(), Feed()
    first = f1.poll("x")
    assert f1.poll("x") == first
    assert f1.poll("y") != first
    assert f2.poll("x") != first


def test_throttle_per_instance_needs_weakref():
    class Slotted:
        __slots__ = ()

        @throttle(1, per_instance=True)
        def m(self) -> None: ...

    with pytest.raises(TypeError, match="weak-referenceable"):
        Slotted().m()


def test_throttle_per_instance_needs_instance():
    @throttle(1, per_instance=True)
    def f() -> None: ...

    with pytest.raises(TypeError, match="instance"):
        f()


def _capture_buckets(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    made: list[Any] = []
    original = concurrency._Buckets.__init__

    def init(self: Any, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        made.append(self)

    monkeypatch.setattr(concurrency._Buckets, "__init__", init)
    return made


def test_throttle_keys_do_not_pile_up(fake_clock, monkeypatch):
    made = _capture_buckets(monkeypatch)

    @throttle(1, key=lambda n: n)
    def f(n: int) -> int:
        return n

    for n in range(3000):
        f(n)
        fake_clock.advance(2)
    # every window had run out when the sweeps ran: far fewer than 3000 states stay
    assert len(made[0].states()) < 1100


async def test_throttle_async_key(fake_clock):
    @throttle(5, key=lambda k: k)
    async def f(k: str) -> str:
        return k

    assert await f("a") == "a"
    assert await f("b") == "b"


# ---------------------------------------------------------------- debounce


def test_debounce_key_independent_bursts():
    calls: list[tuple[str, int]] = []
    done = threading.Event()

    @debounce(0.05, key=lambda k, *_: k)
    def f(k: str, n: int) -> None:
        calls.append((k, n))
        if len(calls) == 2:
            done.set()

    f("a", 1)
    f("b", 1)
    f("a", 2)
    assert done.wait(2)
    assert sorted(calls) == [("a", 2), ("b", 1)]


def test_debounce_per_instance_flush_and_cancel():
    class Doc:
        def __init__(self) -> None:
            self.saved: list[str] = []

        @debounce(60, per_instance=True)
        def save(self, text: str) -> str:
            self.saved.append(text)
            return text

    a, b = Doc(), Doc()
    a.save("a1")
    b.save("b1")
    assert Doc.save.flush(a) == "a1"  # type: ignore[call-arg]
    assert a.saved == ["a1"] and b.saved == []
    Doc.save.cancel(b)  # type: ignore[call-arg]
    assert Doc.save.flush(b) is None  # type: ignore[call-arg]
    assert b.saved == []
    a.save("a2")
    b.save("b2")
    assert Doc.save.flush() is None  # type: ignore[call-arg]  # all, no single result
    assert a.saved == ["a1", "a2"] and b.saved == ["b2"]
    Doc.save.cancel(Doc())  # never called: no-op


def test_debounce_per_instance_collectable():
    class Doc:
        @debounce(60, per_instance=True)
        def save(self) -> None: ...

    d = Doc()
    d.save()
    d.save.cancel(d)
    ref = weakref.ref(d)
    del d
    gc.collect()
    assert ref() is None


def test_debounce_idle_keys_are_swept(monkeypatch):
    made = _capture_buckets(monkeypatch)

    @debounce(0, key=lambda n: n)
    def f(n: int) -> None: ...

    for n in range(2100):
        f(n)
        f.flush(n)
    assert len(made[0].states()) < 1100


async def test_debounce_async_key():
    seen: list[str] = []

    @debounce(0.02, key=lambda k, *_: k)
    async def f(k: str, v: int) -> str:
        seen.append(f"{k}{v}")
        return f"{k}{v}"

    results = await asyncio.gather(f("a", 1), f("b", 1), f("a", 2))
    assert results == ["a2", "b1", "a2"]
    assert sorted(seen) == ["a2", "b1"]


async def test_debounce_async_per_instance_flush_cancel():
    class Doc:
        @debounce(60, per_instance=True)
        async def save(self, text: str) -> str:
            return text

    a, b = Doc(), Doc()
    ta = asyncio.ensure_future(a.save("a"))
    tb = asyncio.ensure_future(b.save("b"))
    await asyncio.sleep(0)
    assert await Doc.save.flush(a) == "a"  # type: ignore[call-arg]
    assert await ta == "a"
    Doc.save.cancel(b)  # type: ignore[call-arg]
    with pytest.raises(asyncio.CancelledError):
        await tb
    assert await Doc.save.flush(Doc()) is None  # type: ignore[call-arg]


async def test_debounce_async_flush_all():
    class Doc:
        @debounce(60, per_instance=True)
        async def save(self, text: str) -> str:
            return text

    a, b = Doc(), Doc()
    ta = asyncio.ensure_future(a.save("a"))
    tb = asyncio.ensure_future(b.save("b"))
    await asyncio.sleep(0)
    assert await Doc.save.flush() is None  # type: ignore[call-arg]
    assert (await ta, await tb) == ("a", "b")


# ---------------------------------------------------------------- rate_limit


def test_rate_limit_per_instance(fake_clock):
    class Client:
        @rate_limit(1, 60.0, per_instance=True)
        def call(self) -> str:
            return "ok"

    a, b = Client(), Client()
    assert a.call() == "ok"
    assert b.call() == "ok"  # its own budget
    with pytest.raises(RateLimitExceeded):
        a.call()
    assert limiter_of(Client.call, instance=a) is not limiter_of(Client.call, instance=b)
    limiter = limiter_of(Client.call, instance=a)
    assert limiter is not None
    limiter.reset()
    assert a.call() == "ok"
    assert limiter_of(Client.call, instance=Client()) is None  # never called


def test_rate_limit_per_instance_collectable_and_unhashable(fake_clock):
    class Client:
        __hash__ = None  # type: ignore[assignment]

        @rate_limit(1, 60.0, per_instance=True)
        def call(self) -> None: ...

    c = Client()
    c.call()
    ref = weakref.ref(c)
    del c
    gc.collect()
    assert ref() is None


async def test_rate_limit_per_instance_async_with_key(fake_clock):
    class Client:
        @rate_limit(1, 60.0, per_instance=True, key=lambda self, user: user)
        async def call(self, user: str) -> str:
            return user

    a, b = Client(), Client()
    assert await a.call("u") == "u"
    assert await a.call("v") == "v"
    assert await b.call("u") == "u"
    with pytest.raises(RateLimitExceeded):
        await a.call("u")


def test_limiter_of_instance_without_per_instance(fake_clock):
    @rate_limit(1, 60.0)
    def f() -> None: ...

    assert limiter_of(f, instance=object()) is limiter_of(f)


_: Any = (time,)  # keep import used by later sections


# ---------------------------------------------------------------- memoize(singleflight=)


def _race(fn: Any, n: int = 8) -> list[Any]:
    barrier = threading.Barrier(n)
    out: list[Any] = [None] * n

    def worker(i: int) -> None:
        barrier.wait()
        try:
            out[i] = fn()
        except BaseException as exc:
            out[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_memoize_default_may_compute_twice_but_singleflight_does_not():
    calls = []

    @memoize(singleflight=True)
    def slow(x: int) -> int:
        calls.append(x)
        time.sleep(0.1)
        return x * 2

    assert _race(lambda: slow(4)) == [8] * 8
    assert calls == [4]
    info = slow.cache_info()
    assert info.misses == 1 and info.hits == 7
    assert slow(4) == 8 and calls == [4]
    assert slow.cache_parameters()["singleflight"] is True


def test_memoize_without_singleflight_computes_concurrently():
    calls = []

    @memoize
    def slow(x: int) -> int:
        calls.append(x)
        time.sleep(0.1)
        return x

    _race(lambda: slow(1))
    assert len(calls) > 1  # the documented default race


def test_memoize_singleflight_different_keys_run_in_parallel():
    calls = []

    @memoize(singleflight=True)
    def slow(x: int) -> int:
        calls.append(x)
        time.sleep(0.05)
        return x

    idx = iter(range(4))
    lock = threading.Lock()

    def one() -> int:
        with lock:
            i = next(idx)
        return slow(i)

    assert sorted(_race(one, 4)) == [0, 1, 2, 3]
    assert sorted(calls) == [0, 1, 2, 3]


def test_memoize_singleflight_shares_exception_and_recovers():
    calls = []

    @memoize(singleflight=True)
    def boom() -> int:
        calls.append(1)
        time.sleep(0.1)
        if len(calls) == 1:
            raise ValueError("nope")
        return 7

    out = _race(boom, 4)
    assert all(isinstance(o, ValueError) for o in out) and len(calls) == 1
    assert boom() == 7  # failures are not cached: the next call computes again


def test_memoize_singleflight_cached_exception():
    calls = []

    @memoize(singleflight=True, cache_exceptions=KeyError)
    def boom() -> int:
        calls.append(1)
        time.sleep(0.05)
        raise KeyError("k")

    assert all(isinstance(o, KeyError) for o in _race(boom, 4))
    with pytest.raises(KeyError):
        boom()
    assert len(calls) == 1


def test_memoize_singleflight_reentrant_does_not_deadlock():
    @memoize(singleflight=True)
    def fib(n: int) -> int:
        return n if n < 2 else fib(n - 1) + fib(n - 2)

    assert fib(10) == 55


def test_memoize_singleflight_same_key_reentrant_computes_directly():
    state = {"entered": False}

    @memoize(singleflight=True)
    def f(n: int) -> int:
        if not state["entered"]:
            state["entered"] = True
            return f(n) + 1  # same key on the computing thread: no self-wait
        return n

    assert f(5) == 6


def test_memoize_singleflight_per_instance_and_copy_result():
    class Svc:
        def __init__(self) -> None:
            self.calls = 0

        @memoize(singleflight=True, per_instance=True, copy_result=True)
        def load(self) -> list[int]:
            self.calls += 1
            time.sleep(0.1)
            return [1]

    a, b = Svc(), Svc()
    out = _race(a.load, 4)
    assert out == [[1]] * 4 and len({id(o) for o in out}) == 4  # private copies
    assert a.calls == 1
    assert b.load() == [1] and b.calls == 1  # another instance computes by itself


def test_memoize_singleflight_with_store():
    class DictStore:
        def __init__(self) -> None:
            self.d: dict[Any, Any] = {}

        def get(self, key: Any) -> Any:
            return self.d.get(key)

        def set(self, key: Any, value: Any, ttl: float | None) -> None:
            self.d[key] = value

        def delete(self, key: Any) -> bool:
            return self.d.pop(key, None) is not None

        def clear(self) -> None:
            self.d.clear()

        def __len__(self) -> int:
            return len(self.d)

    calls = []

    @memoize(singleflight=True, store=DictStore())
    def slow() -> int:
        calls.append(1)
        time.sleep(0.1)
        return 1

    assert _race(slow, 4) == [1] * 4 and calls == [1]


def test_memoize_singleflight_cleared_during_flight():
    started, release = threading.Event(), threading.Event()
    calls = []

    @memoize(singleflight=True)
    def slow() -> int:
        calls.append(1)
        started.set()
        release.wait(2)
        return 1

    t = threading.Thread(target=slow)
    t.start()
    assert started.wait(2)
    slow.cache_clear()  # drops stats; the stored result is discarded as stale
    release.set()
    t.join()
    assert slow.cache_info().currsize == 0


def test_memoize_singleflight_validates():
    with pytest.raises(TypeError, match="singleflight"):
        memoize(singleflight=1)  # type: ignore[arg-type]


async def test_memoize_singleflight_async_unchanged():
    calls = []

    @memoize(singleflight=True)
    async def f(x: int) -> int:
        calls.append(x)
        await asyncio.sleep(0.05)
        return x

    assert await asyncio.gather(*(f(1) for _ in range(5))) == [1] * 5
    assert calls == [1]
