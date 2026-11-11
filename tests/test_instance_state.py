"""``key=`` / ``per_instance=`` for throttle, debounce and rate_limit; memoize(singleflight=)."""

from __future__ import annotations

import asyncio
import gc
import threading
import time
import weakref
from typing import Any

import pytest

from python_wrap import concurrency, debounce, rate_limit, throttle
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
