"""Tests for python_wrap.aio."""

from __future__ import annotations

import asyncio
import sys
import warnings
from collections.abc import AsyncIterator, Iterator
from contextlib import aclosing
from typing import Any

import pytest

from python_wrap.aio import RaceFailed, amap_iter, gather_limit, map_async, race, run_sync
from python_wrap.errors import PythonWrapError, TimeoutExpired
from python_wrap.testing import fast_mode


def other_tasks() -> set[asyncio.Task[Any]]:
    return {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}


async def assert_no_tasks_left() -> None:
    await asyncio.sleep(0)
    assert other_tasks() == set()


class Tracker:
    """Records how many calls run at once; each call waits for its own gate."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.started: list[int] = []
        self.cancelled: list[int] = []
        self.gates: dict[int, asyncio.Event] = {}

    def gate(self, i: int) -> asyncio.Event:
        return self.gates.setdefault(i, asyncio.Event())

    async def run(self, i: int, *, fail: bool = False) -> int:
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.append(i)
        try:
            await self.gate(i).wait()
        except asyncio.CancelledError:
            self.cancelled.append(i)
            raise
        finally:
            self.active -= 1
        if fail:
            raise ValueError(i)
        return i * 10

    def open(self, *indices: int) -> None:
        for i in indices:
            self.gate(i).set()


async def spin(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------------------
# gather_limit
# ---------------------------------------------------------------------------------------


async def test_gather_limit_keeps_input_order_and_caps_concurrency() -> None:
    tr = Tracker()
    job = asyncio.ensure_future(gather_limit(*(tr.run(i) for i in range(6)), limit=2))
    await spin()
    assert tr.started == [0, 1]  # later coroutines not started yet
    tr.open(1)
    await spin()
    assert tr.started == [0, 1, 2]
    tr.open(5, 4, 3, 2, 0)
    assert await job == [0, 10, 20, 30, 40, 50]
    assert tr.peak == 2
    await assert_no_tasks_left()


async def test_gather_limit_empty_and_validation() -> None:
    assert await gather_limit(limit=3) == []
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="limit"):
            await gather_limit(limit=bad)  # type: ignore[arg-type]


async def test_gather_limit_first_error_cancels_rest_and_closes_unstarted() -> None:
    tr = Tracker()
    tr.open(1)
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # "coroutine was never awaited" would fail
        with pytest.raises(ValueError, match="1"):
            await gather_limit(tr.run(0), tr.run(1, fail=True), tr.run(2), tr.run(3), limit=2)
    assert tr.started == [0, 1]
    assert tr.cancelled == [0]
    await assert_no_tasks_left()


async def test_gather_limit_return_exceptions() -> None:
    tr = Tracker()
    tr.open(0, 1)
    victim = asyncio.ensure_future(tr.run(2))
    await spin()
    victim.cancel()
    results = await gather_limit(
        tr.run(0), tr.run(1, fail=True), victim, limit=3, return_exceptions=True
    )
    assert results[0] == 0
    assert isinstance(results[1], ValueError)
    assert isinstance(results[2], asyncio.CancelledError)
    await assert_no_tasks_left()


async def test_gather_limit_accepts_tasks_futures_and_duplicates() -> None:
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[int] = loop.create_future()
    loop.call_soon(fut.set_result, 7)

    async def five() -> int:
        return 5

    task = asyncio.ensure_future(five())
    assert await gather_limit(task, fut, fut, five(), limit=1) == [5, 7, 7, 5]
    await assert_no_tasks_left()


async def test_gather_limit_cancels_unstarted_futures_on_error() -> None:
    tr = Tracker()
    tr.open(0)
    late = asyncio.ensure_future(tr.run(9))
    with pytest.raises(ValueError):
        await gather_limit(tr.run(0, fail=True), late, limit=1)
    assert late.cancelled()
    await assert_no_tasks_left()


async def test_gather_limit_caller_cancellation_cancels_everything() -> None:
    tr = Tracker()
    job = asyncio.ensure_future(gather_limit(*(tr.run(i) for i in range(4)), limit=3))
    await spin()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert sorted(tr.cancelled) == [0, 1, 2]
    await assert_no_tasks_left()


# ---------------------------------------------------------------------------------------
# map_async
# ---------------------------------------------------------------------------------------


async def double(n: int) -> int:
    await asyncio.sleep(0)
    return 2 * n


async def agen(n: int) -> AsyncIterator[int]:
    for i in range(n):
        await asyncio.sleep(0)
        yield i


async def test_map_async_sync_and_async_iterables() -> None:
    assert await map_async(double, range(5), limit=2) == [0, 2, 4, 6, 8]
    assert await map_async(double, agen(5), limit=2) == [0, 2, 4, 6, 8]
    assert await map_async(double, [], limit=2) == []
    await assert_no_tasks_left()


async def test_map_async_unordered_is_completion_order() -> None:
    tr = Tracker()
    job = asyncio.ensure_future(map_async(tr.run, range(3), limit=3, ordered=False))
    await spin()
    tr.open(2)
    await spin()
    tr.open(0)
    await spin()
    tr.open(1)
    assert await job == [20, 0, 10]


async def test_map_async_pulls_input_lazily_and_stops_on_error() -> None:
    pulled: list[int] = []
    tr = Tracker()

    def items() -> Iterator[int]:
        i = 0
        while True:  # infinite
            pulled.append(i)
            yield i
            i += 1

    async def work(i: int) -> int:
        return await tr.run(i, fail=i == 1)

    job = asyncio.ensure_future(map_async(work, items(), limit=3))
    await spin()
    assert pulled == [0, 1, 2]
    tr.open(1)
    with pytest.raises(ValueError):
        await job
    assert sorted(tr.cancelled) == [0, 2]
    assert len(pulled) <= 4
    await assert_no_tasks_left()


async def test_map_async_sync_raise_in_fn_and_return_exceptions() -> None:
    def fn(i: int) -> Any:
        if i == 1:
            raise KeyError(i)
        return double(i)

    out = await map_async(fn, range(3), limit=2, return_exceptions=True)
    assert out[0] == 0 and out[2] == 4 and isinstance(out[1], KeyError)
    with pytest.raises(KeyError):
        await map_async(fn, range(3), limit=2)
    await assert_no_tasks_left()


async def test_map_async_input_error_cleans_up() -> None:
    tr = Tracker()

    async def broken() -> AsyncIterator[int]:
        yield 0
        raise OSError("input")

    with pytest.raises(OSError, match="input"):
        await map_async(tr.run, broken(), limit=5)
    assert tr.cancelled == tr.started  # cancelled (if it even started)
    await assert_no_tasks_left()


# ---------------------------------------------------------------------------------------
# amap_iter
# ---------------------------------------------------------------------------------------


async def test_amap_iter_ordered_buffer_is_bounded() -> None:
    tr = Tracker()
    pulled: list[int] = []

    def items() -> Iterator[int]:
        for i in range(10):
            pulled.append(i)
            yield i

    out: list[int] = []

    async def consume() -> None:
        async with aclosing(amap_iter(tr.run, items(), limit=2)) as results:
            async for value in results:
                out.append(value)

    job = asyncio.ensure_future(consume())
    await spin()
    tr.open(1)  # finishes early but must wait for 0 in the buffer
    await spin()
    assert pulled == [0, 1] and out == []
    tr.open(0)
    await spin()
    assert out == [0, 10]
    tr.open(*range(2, 10))
    await job
    assert out == [i * 10 for i in range(10)]
    assert tr.peak == 2
    await assert_no_tasks_left()


async def test_amap_iter_unordered_yields_as_completed() -> None:
    tr = Tracker()
    out: list[int] = []

    async def consume() -> None:
        async for value in amap_iter(tr.run, range(3), limit=3, ordered=False):
            out.append(value)

    job = asyncio.ensure_future(consume())
    await spin()
    tr.open(2)
    await spin()
    assert out == [20]
    tr.open(0, 1)
    await job
    assert out == [20, 0, 10]


async def test_amap_iter_early_break_cancels_in_flight() -> None:
    tr = Tracker()
    tr.open(0)
    source = agen(100)
    async with aclosing(amap_iter(tr.run, source, limit=3)) as results:
        async for value in results:
            assert value == 0
            break
    await source.aclose()  # the input belongs to the caller
    assert sorted(tr.cancelled) == sorted(set(tr.started) - {0})
    assert len(tr.cancelled) >= 2
    await assert_no_tasks_left()


async def test_amap_iter_consumer_cancellation_cleans_up() -> None:
    tr = Tracker()

    async def consume() -> None:
        async with aclosing(amap_iter(tr.run, range(5), limit=2)) as results:
            async for _ in results:
                pass

    job = asyncio.ensure_future(consume())
    await spin()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert sorted(tr.cancelled) == [0, 1]
    await assert_no_tasks_left()


async def test_amap_iter_errors_in_order_or_as_values() -> None:
    async def maybe_fail(i: int) -> int:
        if i == 2:
            raise ValueError(i)
        return i

    out: list[int] = []
    with pytest.raises(ValueError):
        async for value in amap_iter(maybe_fail, range(5), limit=5):
            out.append(value)
    assert out == [0, 1]
    got = [v async for v in amap_iter(maybe_fail, range(4), limit=2, return_exceptions=True)]
    assert got[:2] == [0, 1] and isinstance(got[2], ValueError) and got[3] == 3
    await assert_no_tasks_left()


async def test_amap_iter_validates_limit_eagerly() -> None:
    with pytest.raises(ValueError):
        amap_iter(double, range(3), limit=0)


# ---------------------------------------------------------------------------------------
# race
# ---------------------------------------------------------------------------------------


async def test_race_first_success_wins_and_losers_are_cancelled() -> None:
    tr = Tracker()
    job = asyncio.ensure_future(race(tr.run(0), tr.run(1), tr.run(2)))
    await spin()
    tr.open(1)
    assert await job == 10
    assert sorted(tr.cancelled) == [0, 2]
    await assert_no_tasks_left()


async def test_race_failures_do_not_end_the_race() -> None:
    tr = Tracker()
    tr.open(0)
    job = asyncio.ensure_future(race(tr.run(0, fail=True), tr.run(1)))
    await spin()
    assert not job.done()
    tr.open(1)
    assert await job == 10


async def test_race_ties_go_to_the_earliest_awaitable() -> None:
    async def value(v: int) -> int:
        return v

    assert await race(value(1), value(2)) == 1
    await assert_no_tasks_left()


async def test_race_all_fail_raises_group_in_input_order() -> None:
    tr = Tracker()
    job = asyncio.ensure_future(race(tr.run(0, fail=True), tr.run(1, fail=True)))
    await spin()
    tr.open(1)
    await spin()
    tr.open(0)
    with pytest.raises(RaceFailed) as info:
        await job
    assert [e.args for e in info.value.exceptions] == [(0,), (1,)]
    assert isinstance(info.value, PythonWrapError)
    if sys.version_info >= (3, 11):
        assert isinstance(info.value, ExceptionGroup)  # noqa: F821
        match, rest = info.value.split(ValueError)
        assert isinstance(match, RaceFailed) and rest is None
    await assert_no_tasks_left()


async def test_race_timeout(fake_clock: Any) -> None:
    tr = Tracker()
    with pytest.raises(TimeoutExpired) as info:
        await race(tr.run(0), tr.run(1), timeout=0.01)
    assert info.value.seconds == 0.01
    assert isinstance(info.value, TimeoutError)
    assert fake_clock.sleeps == []  # a deadline runs on real time, not the fake clock
    assert sorted(tr.cancelled) == [0, 1]
    await assert_no_tasks_left()


async def test_race_timeout_is_not_instant_under_fast_mode() -> None:
    async def slow() -> str:
        await asyncio.sleep(0.02)  # real I/O, well inside the timeout
        return "ok"

    with fast_mode():
        assert await race(slow(), timeout=5) == "ok"
    await assert_no_tasks_left()


async def test_race_winner_before_timeout() -> None:
    async def value() -> str:
        return "ok"

    assert await race(value(), timeout=60) == "ok"
    await assert_no_tasks_left()


async def test_race_base_exception_propagates_at_once() -> None:
    class Stop(BaseException):
        pass

    async def stop() -> None:
        raise Stop

    tr = Tracker()
    with pytest.raises(Stop):
        await race(tr.run(0), stop())
    assert tr.cancelled == [0]
    await assert_no_tasks_left()


async def test_race_all_cancelled_and_empty() -> None:
    tr = Tracker()
    a = asyncio.ensure_future(tr.run(0))
    await spin()
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await race(a)
    with pytest.raises(ValueError):
        await race()


async def test_race_caller_cancellation_cancels_contestants() -> None:
    tr = Tracker()
    job = asyncio.ensure_future(race(tr.run(0), tr.run(1), timeout=60))
    await spin()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert sorted(tr.cancelled) == [0, 1]
    await assert_no_tasks_left()


async def test_cleanup_finishes_even_if_cancelled_again() -> None:
    """A second cancel during cleanup still waits for the children."""
    finished: list[str] = []

    async def stubborn() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            finished.append("child")
            raise

    job = asyncio.ensure_future(race(stubborn()))
    await spin(2)
    job.cancel()
    await asyncio.sleep(0)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert finished == ["child"]
    await assert_no_tasks_left()


# ---------------------------------------------------------------------------------------
# run_sync
# ---------------------------------------------------------------------------------------


async def add(a: int, b: int = 0) -> int:
    await asyncio.sleep(0)
    return a + b


class Awaitable42:
    def __await__(self) -> Any:
        return add(40, 2).__await__()


def test_run_sync_forms() -> None:
    assert run_sync(add(1, 2)) == 3
    assert run_sync(add, 1, b=5) == 6
    assert run_sync(lambda: Awaitable42()) == 42


def test_run_sync_rejects_bad_input() -> None:
    with pytest.raises(TypeError, match="no extra arguments"):
        run_sync(add(1), 2)  # type: ignore[call-overload]
    with pytest.raises(TypeError, match="coroutine or an async callable"):
        run_sync(42)  # type: ignore[call-overload]
    with pytest.raises(TypeError, match="did not return an awaitable"):
        run_sync(lambda: 1)  # type: ignore[arg-type, return-value]


async def test_run_sync_refuses_inside_a_running_loop() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(RuntimeError, match="event loop is running"):
            run_sync(add(1, 2))
        with pytest.raises(RuntimeError, match="event loop is running"):
            run_sync(add, 1, 2)
