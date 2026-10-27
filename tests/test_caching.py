from __future__ import annotations

import asyncio
import copy
import datetime
import inspect
import pickle
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from conftest import FakeClock
from python_wrap import _core
from python_wrap.caching import CacheInfo, cache, cached_property, invalidate, memoize, once
from python_wrap.testing import overrides


def test_bare_and_call_forms() -> None:
    calls: list[int] = []

    def f(x: int) -> int:
        calls.append(x)
        return x * 2

    g = memoize(f)
    assert g(2) == 4 and g(2) == 4
    assert calls == [2]
    assert g.__name__ == "f"

    @memoize
    def h(x: int) -> int:
        return x

    assert h(1) == 1


def test_key_normalizes_defaults_and_kwargs() -> None:
    calls = []

    @memoize
    def f(x: int, y: int = 5) -> int:
        calls.append((x, y))
        return x + y

    assert f(1) == f(x=1) == f(1, 5) == f(1, y=5) == 6
    assert len(calls) == 1
    assert f.cache_info() == CacheInfo(hits=3, misses=1, maxsize=None, currsize=1)


def test_unhashable_builtins_are_converted() -> None:
    calls = []

    @memoize
    def f(*args: Any, **kwargs: Any) -> int:
        calls.append(1)
        return len(calls)

    assert f([1, [2]], {"a": [1]}, {1, 2}) == f([1, [2]], {"a": [1]}, {2, 1})
    assert len(calls) == 1
    assert f([1]) != f((1,))  # list and tuple do not collide
    assert f({"a": 1, "b": 2}) == f({"b": 2, "a": 1})
    assert f((1, [2])) == f((1, [2]))


def test_unhashable_other_raises_type_error() -> None:
    class Bad:
        __hash__ = None  # type: ignore[assignment]

    @memoize
    def f(x: object) -> None: ...

    with pytest.raises(TypeError, match="key="):
        f(Bad())


def test_custom_key() -> None:
    calls = []

    @memoize(key=lambda a, b: a)
    def f(a: int, b: int) -> int:
        calls.append(1)
        return b

    assert f(1, 10) == 10
    assert f(1, 20) == 10
    assert f(2, 20) == 20


def test_typed() -> None:
    @memoize(typed=True)
    def f(x: Any) -> str:
        return type(x).__name__

    assert f(1) == "int"
    assert f(1.0) == "float"

    @memoize
    def g(x: Any) -> str:
        return type(x).__name__

    assert g(1) == "int"
    assert g(1.0) == "int"


def test_lru_eviction() -> None:
    calls = []

    @memoize(maxsize=2)
    def f(x: int) -> int:
        calls.append(x)
        return x

    f(1)
    f(2)
    f(1)  # 1 is now most recent
    f(3)  # evicts 2
    assert f.cache_info().currsize == 2
    f(1)
    assert calls == [1, 2, 3]
    f(2)
    assert calls == [1, 2, 3, 2]


def test_ttl(fake_clock: FakeClock) -> None:
    calls = []

    @memoize(ttl=10)
    def f(x: int) -> int:
        calls.append(x)
        return x

    f(1)
    fake_clock.advance(9)
    f(1)
    assert calls == [1]
    fake_clock.advance(1)
    f(1)
    assert calls == [1, 1]
    fake_clock.advance(20)
    assert f.cache_info().currsize == 0


def test_ttl_timedelta(fake_clock: FakeClock) -> None:
    calls = []

    @memoize(ttl=datetime.timedelta(seconds=5))
    def f() -> int:
        calls.append(1)
        return 1

    f()
    fake_clock.advance(6)
    f()
    assert len(calls) == 2


def test_invalid_options() -> None:
    with pytest.raises(ValueError):
        memoize(maxsize=0)
    with pytest.raises(ValueError):
        memoize(ttl=0)


def test_exceptions_not_cached() -> None:
    calls = []

    @memoize
    def f() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError
        return 7

    with pytest.raises(RuntimeError):
        f()
    assert f() == 7
    assert f() == 7
    assert len(calls) == 2


def test_cache_clear_and_delete() -> None:
    calls = []

    @memoize
    def f(x: int, y: int = 0) -> int:
        calls.append(x)
        return x

    f(1)
    f(2)
    assert f.cache_delete(1) is True
    assert f.cache_delete(x=1, y=0) is False
    f(1)
    assert calls == [1, 2, 1]
    f.cache_clear()
    assert f.cache_info() == CacheInfo(0, 0, None, 0)


def test_methods_are_per_instance() -> None:
    class A:
        def __init__(self, n: int) -> None:
            self.n = n

        @memoize
        def get(self, x: int) -> int:
            return self.n + x

    a, b = A(1), A(10)
    assert a.get(1) == 2
    assert b.get(1) == 11
    assert a.get(1) == 2
    assert A.get.cache_info().hits == 1


def test_thread_safety() -> None:
    calls = []

    @memoize
    def f(x: int) -> int:
        calls.append(x)
        return x * x

    def work() -> None:
        for i in range(50):
            assert f(i) == i * i

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert f.cache_info().currsize == 50


def test_cache_alias_and_independent_stores(fake_clock: FakeClock) -> None:
    deco = cache(10)
    calls = []

    @deco
    def a(x: int) -> int:
        calls.append("a")
        return x

    @deco
    def b(x: int) -> int:
        calls.append("b")
        return x

    a(1)
    b(1)
    a(1)
    assert calls == ["a", "b"]
    fake_clock.advance(11)
    a(1)
    assert calls == ["a", "b", "a"]

    @cache(datetime.timedelta(seconds=1), maxsize=1)
    def c(x: int) -> int:
        return x

    c(1)
    c(2)
    assert c.cache_info().currsize == 1


async def test_async_memoize_and_concurrent_sharing() -> None:
    calls = []
    gate = asyncio.Event()

    @memoize
    async def f(x: int) -> int:
        calls.append(x)
        await gate.wait()
        return x + 1

    assert inspect.iscoroutinefunction(f)
    tasks = [asyncio.ensure_future(f(1)) for _ in range(5)]
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(*tasks) == [2] * 5
    assert calls == [1]
    assert await f(x=1) == 2
    assert calls == [1]
    assert f.cache_info().misses == 1


async def test_async_exception_not_cached_and_cancel_isolated() -> None:
    calls = []

    @memoize
    async def f() -> int:
        calls.append(1)
        await asyncio.sleep(0)
        if len(calls) == 1:
            raise RuntimeError
        return 3

    with pytest.raises(RuntimeError):
        await f()
    assert await f() == 3

    gate = asyncio.Event()

    @memoize
    async def g() -> int:
        await gate.wait()
        return 9

    t1 = asyncio.ensure_future(g())
    t2 = asyncio.ensure_future(g())
    await asyncio.sleep(0)
    t1.cancel()
    await asyncio.sleep(0)
    gate.set()
    assert await t2 == 9


async def test_async_cache_clear_during_flight() -> None:
    gate = asyncio.Event()

    @memoize
    async def f() -> int:
        await gate.wait()
        return 1

    t = asyncio.ensure_future(f())
    await asyncio.sleep(0)
    f.cache_clear()
    gate.set()
    assert await t == 1
    assert f.cache_info().currsize == 0


# ------------------------------------------------------------------------------ once


def test_once_sync() -> None:
    calls = []

    @once
    def init(x: int = 0) -> list[int]:
        calls.append(x)
        return [x]

    assert init.called is False
    first = init(1)
    assert init(2) is first
    assert calls == [1]
    assert init.called is True
    init.reset()
    assert init.called is False
    assert init(3) == [3]
    assert once()(lambda: 5)() == 5


def test_once_failure_retries() -> None:
    calls = []

    @once
    def f() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError
        return 4

    with pytest.raises(RuntimeError):
        f()
    assert f.called is False
    assert f() == 4
    assert f() == 4
    assert len(calls) == 2


def test_once_threads() -> None:
    calls = []
    barrier = threading.Barrier(8)

    @once
    def f() -> int:
        calls.append(1)
        return 1

    def work() -> None:
        barrier.wait()
        f()

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1


async def test_once_async() -> None:
    calls = []
    gate = asyncio.Event()

    @once
    async def f() -> int:
        calls.append(1)
        await gate.wait()
        return 8

    assert inspect.iscoroutinefunction(f)
    tasks = [asyncio.ensure_future(f()) for _ in range(4)]
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(*tasks) == [8] * 4
    assert await f() == 8
    assert calls == [1]
    assert f.called is True
    f.reset()
    assert f.called is False
    assert await f() == 8
    assert calls == [1, 1]


async def test_once_async_failure_retries() -> None:
    calls = []

    @once
    async def f() -> int:
        calls.append(1)
        await asyncio.sleep(0)
        if len(calls) == 1:
            raise ValueError
        return 2

    with pytest.raises(ValueError):
        await f()
    assert f.called is False
    assert await f() == 2


# ------------------------------------------------------------- review regressions


def _run_in_threads(make_coro: Any, count: int = 2) -> list[Any]:
    out: list[Any] = []

    def worker() -> None:
        try:
            out.append(asyncio.run(make_coro()))
        except Exception as exc:  # pragma: no cover - only on regression
            out.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def test_async_memoize_concurrent_across_event_loops() -> None:
    @memoize
    async def slow(x: int) -> int:
        await asyncio.sleep(0.03)
        return x

    assert _run_in_threads(lambda: slow(1)) == [1, 1]


def test_async_once_concurrent_across_event_loops() -> None:
    @once
    async def init() -> int:
        await asyncio.sleep(0.03)
        return 7

    assert _run_in_threads(init) == [7, 7]


def test_memoize_ttl_without_maxsize_sweeps_expired_entries(fake_clock: FakeClock) -> None:
    @memoize(ttl=1)
    def f(x: int) -> int:
        return x

    for i in range(1000):
        f(i)
        fake_clock.advance(2)
    sweep = next(
        c.cell_contents
        for c in f.cache_info.__closure__  # type: ignore[attr-defined]
        if getattr(c.cell_contents, "__name__", "") == "sweep"
    )
    store = next(
        c.cell_contents for c in sweep.__closure__ if hasattr(c.cell_contents, "move_to_end")
    )
    assert len(store) <= 130


def test_memoize_without_signature_keeps_keyword_names_in_the_key() -> None:
    class NoSignature:
        __signature__ = "not a signature"  # inspect.signature() raises TypeError

        def __call__(self, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
            return (args, kwargs)

    m = memoize(NoSignature())
    assert m(1, b=2) == ((1,), {"b": 2})
    assert m(1, 2) == ((1, 2), {})  # not the cached f(1, b=2)
    assert m(1, c=2) == ((1,), {"c": 2})
    assert m(1, b=2) == ((1,), {"b": 2})
    assert m.cache_info().hits == 1


def test_memoized_bound_method_keeps_parameter_types() -> None:
    class C:
        @memoize
        def m(self, x: int) -> int:
            return x

    assert C().m(2) == 2
    # mypy (warn_unused_ignores) fails if bound methods lose their parameter types
    assert C().m("wrong") == "wrong"  # type: ignore[arg-type]


def test_async_memoize_failure_after_all_callers_cancelled_is_retrieved(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import gc

    @memoize
    async def boom() -> None:
        await asyncio.sleep(0.02)
        raise ValueError("down")

    handler_calls: list[dict[str, Any]] = []

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _l, ctx: handler_calls.append(ctx))
        t = asyncio.create_task(boom())
        await asyncio.sleep(0.005)
        t.cancel()
        await asyncio.sleep(0.05)
        gc.collect()

    asyncio.run(main())
    assert handler_calls == []


def test_memoize_dataclass_method_error_names_self() -> None:
    from dataclasses import dataclass

    @dataclass
    class Repo:
        name: str

        @memoize
        def size(self) -> int:
            return len(self.name)

    with pytest.raises(TypeError, match=r"argument 'self'.*key=.*cached_property"):
        Repo("x").size()


def test_async_once_reset_in_flight_does_not_run_twice() -> None:
    calls = 0

    @once
    async def init() -> int:
        nonlocal calls
        calls += 1
        n = calls
        await asyncio.sleep(0.02)
        if n == 1:
            raise ValueError("old run fails")
        return n

    async def main() -> list[int]:
        first = asyncio.create_task(init())
        await asyncio.sleep(0.005)
        init.reset()
        second = asyncio.create_task(init())
        with pytest.raises(ValueError):
            await first  # old run ends first and must not clear the new run's task
        third = asyncio.create_task(init())
        return [await second, await third]

    assert asyncio.run(main()) == [2, 2]
    assert calls == 2


# ------------------------------------------------------- memoize: new options (0.2)


def test_cache_exceptions_cached_and_reraised_same_instance(fake_clock: FakeClock) -> None:
    calls = []

    @memoize(cache_exceptions=ValueError, exception_ttl=5)
    def f(x: int) -> int:
        calls.append(x)
        raise ValueError(x)

    with pytest.raises(ValueError) as first:
        f(1)
    with pytest.raises(ValueError) as second:
        f(1)
    assert first.value is second.value
    assert calls == [1]
    assert f.cache_contains(1)
    with pytest.raises(ValueError):
        f.cache_get(1)
    fake_clock.advance(6)
    assert not f.cache_contains(1)
    with pytest.raises(ValueError):
        f(1)
    assert calls == [1, 1]


def test_cache_exceptions_only_matching_and_defaults_to_ttl(fake_clock: FakeClock) -> None:
    calls = []

    @memoize(ttl=10, cache_exceptions=(KeyError, ValueError))
    def f(kind: str) -> None:
        calls.append(kind)
        raise {"key": KeyError, "type": TypeError}[kind]()

    with pytest.raises(KeyError):
        f("key")
    with pytest.raises(KeyError):
        f("key")
    for _ in range(2):
        with pytest.raises(TypeError):
            f("type")
    assert calls == ["key", "type", "type"]
    fake_clock.advance(11)
    with pytest.raises(KeyError):
        f("key")
    assert calls == ["key", "type", "type", "key"]


def test_cache_exceptions_validation() -> None:
    with pytest.raises(TypeError):
        memoize(cache_exceptions=int)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        memoize(ttl=lambda r: 1.0, cache_exceptions=ValueError)
    with pytest.raises(ValueError):
        memoize(exception_ttl=0, cache_exceptions=ValueError)
    # allowed: explicit exception_ttl
    memoize(ttl=lambda r: 1.0, cache_exceptions=ValueError, exception_ttl=1)


async def test_async_cache_exceptions_shared_and_cached() -> None:
    calls = []

    @memoize(cache_exceptions=RuntimeError)
    async def f() -> int:
        calls.append(1)
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    results = await asyncio.gather(f(), f(), return_exceptions=True)
    assert results[0] is results[1]
    with pytest.raises(RuntimeError):
        await f()
    assert calls == [1]


async def test_async_cancelled_error_never_cached() -> None:
    calls = []

    @memoize(cache_exceptions=BaseException)
    async def f() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise asyncio.CancelledError
        return 5

    with pytest.raises(asyncio.CancelledError):
        await f()
    assert await f() == 5


def test_per_item_ttl(fake_clock: FakeClock) -> None:
    @memoize(ttl=lambda r: None if r == "forever" else (0 if r == "skip" else r))
    def f(x: Any) -> Any:
        return x

    f(3)
    f("forever")
    f("skip")
    assert f.cache_info().currsize == 2
    assert f.cache_contains(3) and not f.cache_contains("skip")
    fake_clock.advance(4)
    assert not f.cache_contains(3)
    assert f.cache_contains("forever")
    assert callable(f.cache_parameters()["ttl"])


def test_cache_contains_get_do_not_count_or_compute() -> None:
    calls = []

    @memoize(maxsize=2)
    def f(x: Any, y: int = 0) -> Any:
        calls.append(x)
        return x * 10

    assert not f.cache_contains(1)
    with pytest.raises(KeyError):
        f.cache_get(1)
    f(1)
    f(2)
    assert f.cache_contains(1, y=0) and f.cache_get(x=1) == 10
    f(3)  # evicts 1: cache_get above did not refresh its LRU position
    assert not f.cache_contains(1)
    assert f.cache_info().hits == 0 and f.cache_info().misses == 3
    assert f.cache_contains([1]) is False  # unhashable list args are frozen, not errors


def test_cache_parameters() -> None:
    @memoize(maxsize=3, ttl=datetime.timedelta(seconds=2), typed=True)
    def f(x: int) -> int:
        return x

    assert f.cache_parameters() == {
        "maxsize": 3,
        "ttl": 2.0,
        "typed": True,
        "cache_exceptions": (),
        "exception_ttl": 2.0,
    }


def test_fast_key_path_matches_general_path() -> None:
    calls = []

    @memoize
    def f(a: Any, b: Any) -> int:
        calls.append(1)
        return 1

    f(1, 2)
    f(a=1, b=2)
    f(1, b=2)
    f((1, 2), 3)
    f((1, 2), 3)
    f([1], 2)  # unhashable positional: falls back to freezing
    f([1], 2)
    assert len(calls) == 3
    with pytest.raises(TypeError, match="not hashable"):
        f(object.__new__(type("U", (), {"__hash__": None})), 1)


def test_disable_caching_memoize_cache_once() -> None:
    calls = []

    @memoize
    def m(x: int) -> int:
        calls.append(x)
        return x

    @cache(60)
    def c(x: int) -> int:
        calls.append(x)
        return x

    @once
    def o() -> int:
        calls.append(0)
        return 1

    m(1)
    with overrides(disable_caching=True):
        m(1)
        m(1)
        c(1)
        c(1)
        o()
        o()
        assert m.cache_info().currsize == 1  # nothing written
    assert calls == [1, 1, 1, 1, 1, 0, 0]
    m(1)
    assert calls == [1, 1, 1, 1, 1, 0, 0]  # earlier entry still intact
    assert o() == 1 and o() == 1
    assert calls.count(0) == 3


async def test_disable_caching_async() -> None:
    calls = []

    @memoize
    async def m(x: int) -> int:
        calls.append(x)
        return x

    @once
    async def o() -> int:
        calls.append(0)
        return 1

    with overrides(disable_caching=True):
        await m(1)
        await m(1)
        await o()
        await o()
    assert calls == [1, 1, 0, 0]
    assert m.cache_info().currsize == 0


def _collect() -> tuple[list[_core.Event], Callable[[_core.Event], None]]:
    seen: list[_core.Event] = []
    _core.listeners.append(seen.append)
    return seen, seen.append


def test_events_hit_miss_evicted(fake_clock: FakeClock) -> None:
    seen, listener = _collect()
    try:

        @memoize(maxsize=1, ttl=5)
        def f(x: int) -> int:
            return x

        f(1)
        f(1)
        f(2)  # evicts 1 (lru)
        fake_clock.advance(6)
        f(2)  # expired entry removed on lookup (ttl)
    finally:
        _core.listeners.remove(listener)
    names = [(e.name, e.fields) for e in seen]
    assert names == [
        ("cache.miss", {}),
        ("cache.hit", {}),
        ("cache.miss", {}),
        ("cache.evicted", {"reason": "lru"}),
        ("cache.evicted", {"reason": "ttl"}),
        ("cache.miss", {}),
    ]
    assert all(e.function.endswith("f") for e in seen)


async def test_events_async() -> None:
    seen, listener = _collect()
    try:

        @memoize
        async def f(x: int) -> int:
            await asyncio.sleep(0)
            return x

        await asyncio.gather(f(1), f(1))
        await f(1)
    finally:
        _core.listeners.remove(listener)
    assert [e.name for e in seen] == ["cache.miss", "cache.hit", "cache.hit"]


def test_events_ttl_eviction_from_cache_info(fake_clock: FakeClock) -> None:
    @memoize(ttl=1)
    def f(x: int) -> int:
        return x

    f(1)
    fake_clock.advance(2)
    seen, listener = _collect()
    try:
        assert f.cache_info().currsize == 0
    finally:
        _core.listeners.remove(listener)
    assert [(e.name, e.fields) for e in seen] == [("cache.evicted", {"reason": "ttl"})]


# ------------------------------------------------------------------ cached_property


def test_cached_property_basic_and_class_access() -> None:
    calls = []

    class A:
        @cached_property
        def v(self) -> int:
            """doc"""
            calls.append(1)
            return 42

    a, b = A(), A()
    assert a.v == 42 and a.v == 42 and b.v == 42
    assert len(calls) == 2
    assert A.v is A.__dict__["v"]
    assert A.v.__doc__ == "doc"
    with pytest.raises(AttributeError):
        a.v = 1  # type: ignore[misc]


def test_cached_property_ttl_and_invalidation(fake_clock: FakeClock) -> None:
    calls = []

    class A:
        @cached_property(ttl=datetime.timedelta(seconds=10))
        def v(self) -> int:
            calls.append(1)
            return len(calls)

        @cached_property
        def w(self) -> int:
            return 7

        plain = 1

    a = A()
    assert a.v == 1 and a.v == 1
    fake_clock.advance(11)
    assert a.v == 2
    del a.v
    assert a.v == 3
    assert invalidate(a, "v") == 1
    assert invalidate(a, "v") == 0
    assert a.v == 4
    assert a.w == 7
    assert invalidate(a) == 2
    del a.w  # deleting something never cached is a no-op
    with pytest.raises(AttributeError, match="no cached_property"):
        invalidate(a, "nope")
    with pytest.raises(AttributeError, match="not a cached_property"):
        invalidate(a, "plain")
    with pytest.raises(ValueError):
        cached_property(ttl=0)


def test_cached_property_slots_without_dict() -> None:
    class S:
        __slots__ = ()

        @cached_property
        def v(self) -> int:
            return 1

    with pytest.raises(TypeError, match="__dict__"):
        _ = S().v
    with pytest.raises(TypeError, match="__dict__"):
        invalidate(S())

    class S2:
        __slots__ = ("__dict__",)

        @cached_property
        def v(self) -> int:
            return 1

    assert S2().v == 1


def test_cached_property_thread_safety_single_compute() -> None:
    calls = []
    gate = threading.Barrier(8)

    class A:
        @cached_property
        def v(self) -> int:
            calls.append(1)
            threading.Event().wait(0.02)
            return 5

    a = A()
    out: list[int] = []

    def work() -> None:
        gate.wait()
        out.append(a.v)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert out == [5] * 8 and len(calls) == 1


class _Picklable:
    calls = 0

    @cached_property
    def v(self) -> int:
        type(self).calls += 1
        return type(self).calls


def test_cached_property_copy_and_pickle_do_not_share_state() -> None:
    a = _Picklable()
    assert a.v == 1
    b = copy.copy(a)
    assert b.v == 2 and a.v == 1
    q = pickle.loads(pickle.dumps(a))
    assert q.v == 3
    del q.v
    assert invalidate(q) == 0


def test_cached_property_set_name_conflict_and_failure_not_cached() -> None:
    with pytest.raises((RuntimeError, TypeError)):

        class _Bad:
            a = cached_property(lambda self: 1)
            b = a

    calls = []

    class A:
        @cached_property
        def v(self) -> int:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError
            return 2

    a = A()
    with pytest.raises(RuntimeError):
        _ = a.v
    assert a.v == 2


def test_cached_property_disable_caching() -> None:
    calls = []

    class A:
        @cached_property
        def v(self) -> int:
            calls.append(1)
            return 1

    a = A()
    with overrides(disable_caching=True):
        _ = a.v
        _ = a.v
    assert len(calls) == 2
    _ = a.v
    _ = a.v
    assert len(calls) == 3


async def _await(aw: Awaitable[Any]) -> Any:
    return await aw


async def test_async_cached_property_shares_task_and_caches() -> None:
    calls = []
    gate = asyncio.Event()

    class A:
        @cached_property(ttl=60)
        async def v(self) -> int:
            calls.append(1)
            await gate.wait()
            return 9

    a = A()
    tasks = [asyncio.ensure_future(_await(a.v)) for _ in range(4)]
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(*tasks) == [9] * 4
    assert await a.v == 9
    assert calls == [1]
    del a.v
    assert await a.v == 9
    assert calls == [1, 1]


async def test_async_cached_property_failure_not_cached_and_invalidate_in_flight() -> None:
    calls = []
    gate = asyncio.Event()

    class A:
        @cached_property
        async def v(self) -> int:
            calls.append(1)
            await gate.wait()
            if len(calls) == 1:
                raise RuntimeError
            return len(calls)

    a = A()
    t = asyncio.ensure_future(_await(a.v))
    await asyncio.sleep(0)
    gate.set()
    with pytest.raises(RuntimeError):
        await t
    assert await a.v == 2

    # invalidated while computing: the in-flight result is returned but not stored
    gate.clear()
    b = A()
    calls.clear()
    calls.append(0)  # next call returns 2
    t2 = asyncio.ensure_future(_await(b.v))
    await asyncio.sleep(0)
    assert invalidate(b) == 0  # nothing cached yet, but the in-flight task is dropped
    gate.set()
    assert await t2 == 2
    assert await b.v == 3  # recomputed: nothing was stored


def test_async_cached_property_across_event_loops() -> None:
    class A:
        @cached_property
        async def v(self) -> int:
            await asyncio.sleep(0.02)
            return 4

    a = A()
    assert _run_in_threads(lambda: a.v) == [4, 4]


def test_async_cached_property_disable_caching() -> None:
    calls = []

    class A:
        @cached_property
        async def v(self) -> int:
            calls.append(1)
            return 1

    async def main() -> None:
        a = A()
        with overrides(disable_caching=True):
            await a.v
            await a.v
        assert len(calls) == 2

    asyncio.run(main())


# ------------------------------------------------- eager task factory / multiple loops

needs_eager = pytest.mark.skipif(
    not hasattr(asyncio, "eager_task_factory"), reason="needs asyncio.eager_task_factory (3.12+)"
)


def _run_eager(main: Callable[[], Awaitable[Any]]) -> Any:
    """``asyncio.run(main())`` with the eager task factory, failing fast instead of hanging."""

    async def eager_main() -> Any:
        asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)  # type: ignore[attr-defined]
        return await main()

    out: list[Any] = []
    t = threading.Thread(target=lambda: out.append(asyncio.run(eager_main())), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "deadlocked under eager_task_factory"
    return out[0]


@needs_eager
def test_eager_memoize_no_deadlock_and_no_stale_inflight() -> None:
    calls: list[int] = []

    @memoize(cache_exceptions=KeyError)
    async def f(x: int) -> int:
        calls.append(x)
        if x < 0:
            raise ValueError("no")
        return x * 2

    async def main() -> list[Any]:
        out: list[Any] = [await f(1), await f(1)]
        for _ in range(2):  # a failure is not cached and leaves no finished task behind
            with pytest.raises(ValueError):
                await f(-1)
        f.cache_clear()
        out.append(await f(1))
        out += await asyncio.gather(f(2), f(2))  # suspending-free runs still share/cache
        return out

    assert _run_eager(main) == [2, 2, 2, 4, 4]
    assert calls == [1, -1, -1, 1, 2]


@needs_eager
def test_eager_memoize_suspending_run_is_shared() -> None:
    calls = 0

    @memoize
    async def f() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return calls

    async def main() -> list[int]:
        return list(await asyncio.gather(f(), f(), f()))

    assert _run_eager(main) == [1, 1, 1]
    assert calls == 1


@needs_eager
def test_eager_once_failure_retries_and_success_sticks() -> None:
    calls = 0

    @once
    async def init() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("first fails")
        return calls

    async def main() -> list[int]:
        with pytest.raises(ValueError):
            await init()
        return [await init(), await init()]

    assert _run_eager(main) == [2, 2]
    assert calls == 2


@needs_eager
def test_eager_cached_property_failure_retries_and_caches() -> None:
    calls = 0

    class C:
        @cached_property
        async def v(self) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError("first fails")
            return calls

    async def main() -> list[int]:
        c = C()
        with pytest.raises(ValueError):
            await c.v
        return [await c.v, await c.v]

    assert _run_eager(main) == [2, 2]
    assert calls == 2


def _in_loops(n: int, main: Callable[[], Awaitable[Any]]) -> list[Any]:
    """Run ``asyncio.run(main())`` in ``n`` threads that start together."""
    barrier = threading.Barrier(n)
    out: list[Any] = []
    errors: list[BaseException] = []

    def worker() -> None:
        barrier.wait()
        try:
            out.append(asyncio.run(main()))  # type: ignore[arg-type]
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    return out + errors


def test_async_once_runs_once_across_event_loops() -> None:
    runs = 0
    runs_lock = threading.Lock()

    @once
    async def setup() -> str:
        nonlocal runs
        with runs_lock:
            runs += 1
        await asyncio.sleep(0.03)
        return "ready"

    async def main() -> list[str]:
        return list(await asyncio.gather(setup(), setup()))

    assert _in_loops(4, main) == [["ready", "ready"]] * 4
    assert runs == 1 and setup.called


def test_async_once_failure_shared_across_loops_then_retries() -> None:
    runs = 0
    runs_lock = threading.Lock()

    @once
    async def setup() -> int:
        nonlocal runs
        with runs_lock:
            runs += 1
            n = runs
        await asyncio.sleep(0.03)
        if n == 1:
            raise ValueError("first fails")
        return n

    results = _in_loops(3, setup)
    assert runs == 1
    assert len(results) == 3 and all(isinstance(r, ValueError) for r in results)
    assert not setup.called
    assert asyncio.run(setup()) == 2


def test_async_once_cancelled_owner_lets_other_loop_take_over() -> None:
    runs = 0
    started = threading.Event()
    owner_cancelled = threading.Event()

    @once
    async def setup() -> int:
        nonlocal runs
        runs += 1
        n = runs
        started.set()
        await asyncio.sleep(0.03)
        return n

    async def owner() -> None:
        t = asyncio.ensure_future(setup())
        await asyncio.sleep(0.005)
        t.cancel()  # cancels only the caller; the run itself keeps going ...
        await asyncio.sleep(0)
        for task in asyncio.all_tasks():  # ... so cancel the run task too
            if task is not asyncio.current_task():
                task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        owner_cancelled.set()

    def other() -> int:
        started.wait(1)
        return asyncio.run(setup())

    out: list[int] = []
    th = threading.Thread(target=lambda: out.append(other()))
    th.start()
    asyncio.run(owner())
    th.join(5)
    assert owner_cancelled.is_set()
    assert out == [2] and runs == 2
    assert asyncio.run(setup()) == 2


def test_async_once_takes_over_from_a_closed_loop() -> None:
    calls = 0

    @once
    async def setup() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            await asyncio.sleep(10)  # stranded: its loop is closed while it waits
        return calls

    dead = asyncio.new_event_loop()
    dead.set_exception_handler(lambda _l, _c: None)  # silence 'Task was destroyed'
    stranded = dead.create_task(setup())
    dead.run_until_complete(asyncio.sleep(0.005))  # the run is now in flight on `dead`
    dead.close()
    assert not stranded.done()
    # The stale flight must not block other loops forever: a new loop runs it again.
    assert asyncio.run(asyncio.wait_for(setup(), 1)) == 2
    assert asyncio.run(setup()) == 2 and calls == 2


def test_async_once_reset_while_in_flight_on_another_loop() -> None:
    gate = threading.Event()

    @once
    async def setup() -> int:
        await asyncio.sleep(0.03)
        return 1

    out: list[int] = []
    th = threading.Thread(target=lambda: (out.append(asyncio.run(setup())), gate.set()))
    th.start()
    asyncio.run(asyncio.sleep(0.01))
    setup.reset()  # the in-flight result is discarded for later callers
    gate.wait(1)
    th.join(5)
    assert out == [1]
    assert not setup.called


@needs_eager
def test_eager_memoize_task_creation_failure_cleans_up(monkeypatch: pytest.MonkeyPatch) -> None:
    @memoize
    async def f() -> int:
        return 1

    real = asyncio.ensure_future
    fail = [True]

    def flaky(coro: Any, **kw: Any) -> Any:
        if fail.pop() if fail else False:
            raise RuntimeError("cannot schedule")
        return real(coro, **kw)

    monkeypatch.setattr(asyncio, "ensure_future", flaky)

    async def main() -> int:
        with pytest.raises(RuntimeError, match="cannot schedule"):
            await f()
        return await f()  # nothing stale is left in flight

    assert _run_eager(main) == 1


def test_cache_lookups_on_a_method_take_the_instance() -> None:
    class Repo:
        @memoize
        def load(self, k: str) -> str:
            return k.upper()

    repo = Repo()
    assert repo.load("a") == "A"
    assert repo.load.cache_contains(repo, "a")
    assert repo.load.cache_get(repo, "a") == "A"
    assert Repo.load.cache_get(repo, "a") == "A"
    assert repo.load.cache_delete(repo, "a")
    assert not repo.load.cache_contains(repo, "a")
