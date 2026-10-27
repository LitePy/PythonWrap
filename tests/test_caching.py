from __future__ import annotations

import asyncio
import datetime
import inspect
import threading
from typing import Any

import pytest

from conftest import FakeClock
from python_wrap.caching import CacheInfo, cache, memoize, once


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
