from __future__ import annotations

import asyncio
import inspect
import threading

import pytest

from conftest import FakeClock
from python_wrap import _core
from python_wrap.metrics import count_calls


def test_counts_calls_errors_and_durations(fake_clock: FakeClock) -> None:
    @count_calls
    def work(n: int) -> int:
        _core.sleep(n)
        if n == 99:
            raise ValueError("bad")
        return n

    assert work.stats.calls == 0
    assert work.stats.mean == work.stats.p50 == work.stats.last_duration == 0.0
    for n in (1, 2, 3, 4):
        assert work(n) == n
    with pytest.raises(ValueError):
        work(99)
    s = work.stats
    assert (s.calls, s.errors, s.in_flight) == (5, 1, 0)
    assert s.last_duration == pytest.approx(99)
    assert s.p50 == pytest.approx(3)
    assert s.p99 == pytest.approx(99)
    assert s.mean == pytest.approx((1 + 2 + 3 + 4 + 99) / 5)
    assert "calls=5" in repr(s)
    work.reset()
    assert (s.calls, s.errors, s.mean) == (0, 0, 0.0)


def test_window_is_a_ring_buffer(fake_clock: FakeClock) -> None:
    @count_calls(window=2)
    def work(n: float) -> None:
        _core.sleep(n)

    for n in (10, 1, 3):
        work(n)
    assert work.stats.calls == 3
    assert work.stats.mean == pytest.approx(2)  # only 1 and 3 remain
    assert work.stats.p95 == pytest.approx(3)


def test_invalid_window() -> None:
    with pytest.raises(ValueError):
        count_calls(window=0)


async def test_async_in_flight_and_cancellation() -> None:
    gate = asyncio.Event()

    @count_calls
    async def work() -> str:
        await gate.wait()
        return "ok"

    assert inspect.iscoroutinefunction(work)
    tasks = [asyncio.create_task(work()) for _ in range(3)]
    await asyncio.sleep(0)
    assert work.stats.in_flight == 3
    gate.set()
    assert await asyncio.gather(*tasks) == ["ok"] * 3
    assert work.stats.in_flight == 0 and work.stats.calls == 3

    gate.clear()
    task = asyncio.create_task(work())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert work.stats.errors == 1 and work.stats.in_flight == 0


def test_methods_share_stats() -> None:
    class Svc:
        @count_calls
        def go(self, x: int) -> int:
            return x

        @classmethod
        @count_calls
        def make(cls, x: int) -> int:
            return x

    a, b = Svc(), Svc()
    assert a.go(1) + b.go(2) == 3
    assert Svc.make(5) == 5
    assert Svc.__dict__["go"].stats.calls == 2


def test_thread_safety() -> None:
    @count_calls
    def work() -> None:
        pass

    def run() -> None:
        for _ in range(500):
            work()

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert work.stats.calls == 4000
    assert work.stats.in_flight == 0
