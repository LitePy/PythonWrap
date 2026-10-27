"""Tests for python_wrap.bulkhead."""

from __future__ import annotations

import asyncio
import inspect
import threading
from typing import Any

import pytest

from python_wrap import _core
from python_wrap.bulkhead import Bulkhead, limit_concurrency
from python_wrap.errors import BulkheadFull, PythonWrapError
from python_wrap.testing import fast_mode


def other_tasks() -> set[asyncio.Task[Any]]:
    return {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}


async def spin(n: int = 5) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


def wait_until(predicate: Any, timeout: float = 2.0) -> None:
    done = threading.Event()
    deadline = 0.0
    while not predicate():
        if deadline > timeout:
            raise AssertionError("condition not reached")
        done.wait(0.001)
        deadline += 0.001


# ---------------------------------------------------------------------------------------
# construction
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_concurrent": 0},
        {"max_concurrent": True},
        {"max_concurrent": 1.5},
        {"max_concurrent": 1, "max_waiting": -1},
        {"max_concurrent": 1, "timeout": -1},
    ],
)
def test_invalid_arguments(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Bulkhead(**kwargs)


def test_repr_and_error_details() -> None:
    bh = Bulkhead(1, max_waiting=0, name="db")
    assert repr(bh) == "Bulkhead(name='db', max_concurrent=1, active=0, waiting=0)"
    with bh, pytest.raises(BulkheadFull, match="'db' is full") as info:
        bh.acquire()
    assert info.value.name == "db"
    assert info.value.max_concurrent == 1
    assert isinstance(info.value, PythonWrapError)
    with Bulkhead(1, max_waiting=0) as anon, pytest.raises(BulkheadFull, match=r"^bulkhead is"):
        anon.acquire()


def test_release_without_acquire_raises() -> None:
    bh = Bulkhead(1)
    with pytest.raises(ValueError):
        bh.release()
    assert bh.active == 0


# ---------------------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------------------


def test_sync_caps_concurrency_and_queues() -> None:
    bh = Bulkhead(2)
    gate = threading.Event()
    peak = 0
    lock = threading.Lock()

    @bh
    def work(i: int) -> int:
        nonlocal peak
        with lock:
            peak = max(peak, bh.active)
        gate.wait(2)
        return i

    results: list[int] = []
    threads = [threading.Thread(target=lambda i=i: results.append(work(i))) for i in range(4)]
    for t in threads:
        t.start()
    wait_until(lambda: bh.active == 2 and bh.waiting == 2)
    gate.set()
    for t in threads:
        t.join(2)
    assert sorted(results) == [0, 1, 2, 3]
    assert peak == 2
    assert (bh.active, bh.waiting) == (0, 0)


def test_sync_max_waiting_rejects_immediately() -> None:
    bh = Bulkhead(1, max_waiting=1)
    gate = threading.Event()
    with bh:
        waiter = threading.Thread(target=lambda: bh(gate.wait)(2))
        waiter.start()
        wait_until(lambda: bh.waiting == 1)
        with pytest.raises(BulkheadFull, match="max_waiting=1"):
            bh.acquire()
    gate.set()
    waiter.join(2)
    assert (bh.active, bh.waiting) == (0, 0)


def test_sync_timeout() -> None:
    bh = Bulkhead(1, timeout=0.01)
    with bh, pytest.raises(BulkheadFull, match=r"within 0\.01s"):
        bh.acquire()
    assert (bh.active, bh.waiting) == (0, 0)
    with bh:  # the slot is usable again
        assert bh.active == 1


def test_sync_timeout_acquires_when_slot_frees() -> None:
    bh = Bulkhead(1, timeout=5)
    bh.acquire()
    got = threading.Event()

    def take() -> None:
        with bh:
            got.set()

    t = threading.Thread(target=take)
    t.start()
    wait_until(lambda: bh.waiting == 1)
    bh.release()
    t.join(2)
    assert got.is_set()


def test_sync_exception_releases_slot() -> None:
    bh = Bulkhead(1)

    @bh
    def boom() -> None:
        raise KeyError("x")

    for _ in range(3):
        with pytest.raises(KeyError):
            boom()
    assert bh.active == 0


def test_decorator_on_methods_keeps_metadata() -> None:
    bh = Bulkhead(1)

    class Repo:
        @bh
        def get(self, key: str) -> str:
            """Docs."""
            return key.upper()

        @bh
        async def aget(self, key: str) -> str:
            return key

    assert Repo().get("a") == "A"
    assert Repo.get.__doc__ == "Docs."
    assert inspect.iscoroutinefunction(Repo.aget)
    assert asyncio.run(Repo().aget("b")) == "b"


# ---------------------------------------------------------------------------------------
# async
# ---------------------------------------------------------------------------------------


async def test_async_caps_concurrency() -> None:
    bh = Bulkhead(2)
    gate = asyncio.Event()
    peak = 0

    @bh
    async def work(i: int) -> int:
        nonlocal peak
        peak = max(peak, bh.active)
        await gate.wait()
        return i

    assert inspect.iscoroutinefunction(work)
    job = asyncio.gather(*(work(i) for i in range(5)))
    await spin()
    assert (bh.active, bh.waiting) == (2, 3)
    gate.set()
    assert await job == [0, 1, 2, 3, 4]
    assert peak == 2
    assert (bh.active, bh.waiting) == (0, 0)


async def test_async_timeout_is_not_instant_under_fast_mode() -> None:
    bh = Bulkhead(1, timeout=5)

    @bh
    async def work() -> int:
        await asyncio.sleep(0.02)  # real I/O, well inside the timeout
        return 1

    with fast_mode():
        assert await asyncio.gather(work(), work()) == [1, 1]
    assert (bh.active, bh.waiting) == (0, 0)


async def test_async_max_waiting_zero_fails_fast() -> None:
    bh = Bulkhead(1, max_waiting=0)
    async with bh:
        with pytest.raises(BulkheadFull):
            await bh.acquire_async()
    assert (bh.active, bh.waiting) == (0, 0)


async def test_async_timeout_is_real_time(fake_clock: Any) -> None:
    bh = Bulkhead(1, timeout=0.01)
    async with bh:
        with pytest.raises(BulkheadFull, match=r"within 0\.01s"):
            await bh.acquire_async()
    assert fake_clock.sleeps == []  # a deadline, not a backoff: the fake clock is not used
    assert (bh.active, bh.waiting) == (0, 0)
    async with bh:
        pass
    await spin()
    assert other_tasks() == set()


async def test_async_timeout_acquires_when_slot_frees() -> None:
    bh = Bulkhead(1, timeout=60)
    await bh.acquire_async()
    job = asyncio.ensure_future(bh.acquire_async())
    await spin()
    assert bh.waiting == 1
    bh.release_async()
    await job
    assert (bh.active, bh.waiting) == (1, 0)
    bh.release_async()
    await spin()
    assert other_tasks() == set()


@pytest.mark.parametrize("timeout", [None, 60])
async def test_async_cancel_while_waiting_does_not_leak(timeout: float | None) -> None:
    bh = Bulkhead(1, timeout=timeout)
    await bh.acquire_async()
    job = asyncio.ensure_future(bh.acquire_async())
    await spin()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert bh.waiting == 0
    bh.release_async()
    async with bh:  # the permit was not lost
        assert bh.active == 1
    await spin()
    assert other_tasks() == set()


async def test_async_cancel_racing_with_release_returns_permit() -> None:
    """The slot frees up in the same step the waiter is cancelled: no permit is lost."""
    bh = Bulkhead(1, timeout=60)
    await bh.acquire_async()
    job = asyncio.ensure_future(bh.acquire_async())
    await spin()
    bh.release_async()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    assert (bh.active, bh.waiting) == (0, 0)
    async with bh:
        pass
    async with bh:
        pass


async def test_async_exception_releases_slot() -> None:
    bh = Bulkhead(1)

    @bh
    async def boom() -> None:
        raise KeyError("x")

    for _ in range(3):
        with pytest.raises(KeyError):
            await boom()
    assert bh.active == 0


def test_async_works_across_event_loops() -> None:
    bh = Bulkhead(1)

    @bh
    async def work() -> int:
        await asyncio.sleep(0)
        return bh.active

    assert asyncio.run(work()) == 1
    assert asyncio.run(work()) == 1  # a new loop gets its own semaphore


def test_sync_and_async_sides_are_separate() -> None:
    bh = Bulkhead(1, max_waiting=0)

    async def inside() -> int:
        async with bh:
            return bh.active

    with bh:
        assert asyncio.run(inside()) == 2


# ---------------------------------------------------------------------------------------
# limit_concurrency
# ---------------------------------------------------------------------------------------


async def test_limit_concurrency_async() -> None:
    gate = asyncio.Event()

    @limit_concurrency(2)
    async def fetch(i: int) -> int:
        await gate.wait()
        return i

    assert inspect.iscoroutinefunction(fetch)
    assert fetch.bulkhead.max_concurrent == 2
    assert fetch.bulkhead.name is not None and fetch.bulkhead.name.endswith("fetch")
    job = asyncio.gather(*(fetch(i) for i in range(4)))
    await spin()
    assert (fetch.bulkhead.active, fetch.bulkhead.waiting) == (2, 2)
    gate.set()
    assert await job == [0, 1, 2, 3]


def test_limit_concurrency_sync_and_per_function() -> None:
    @limit_concurrency(1)
    def a() -> int:
        return a.bulkhead.active

    @limit_concurrency(1)
    def b() -> int:
        return b.bulkhead.active

    assert a.bulkhead is not b.bulkhead
    assert a() == 1 and b() == 1


def test_limit_concurrency_on_method() -> None:
    class Client:
        @limit_concurrency(3)
        def call(self, x: int) -> int:
            return x + 1

    assert Client().call(1) == 2
    assert Client().call.bulkhead is Client.call.bulkhead


def test_limit_concurrency_validates() -> None:
    with pytest.raises(ValueError):
        limit_concurrency(0)


# ------------------------------------------------------------------ events and classmethods


@pytest.fixture
def events() -> Any:
    seen: list[_core.Event] = []
    _core.listeners.append(seen.append)
    try:
        yield seen
    finally:
        _core.listeners.remove(seen.append)


def test_rejection_emits_event_with_counts(events: list[_core.Event]) -> None:
    bh = Bulkhead(1, max_waiting=0, name="db")
    reads: list[int] = []

    def listener_reads_bulkhead(event: _core.Event) -> None:
        reads.append(bh.active)  # the lock is not held while listeners run

    _core.listeners.append(listener_reads_bulkhead)
    try:
        with bh, pytest.raises(BulkheadFull):
            bh.acquire()
    finally:
        _core.listeners.remove(listener_reads_bulkhead)
    assert reads == [1]
    assert [(e.name, e.function, e.fields) for e in events] == [
        ("bulkhead.rejected", "db", {"active": 1, "waiting": 0})
    ]


def test_timeout_rejection_emits_event(events: list[_core.Event]) -> None:
    bh = Bulkhead(1, timeout=0.01)
    with bh, pytest.raises(BulkheadFull):
        bh.acquire()
    assert [(e.name, e.function, e.fields) for e in events] == [
        ("bulkhead.rejected", "bulkhead", {"active": 1, "waiting": 0})
    ]


async def test_async_timeout_rejection_emits_event(events: list[_core.Event]) -> None:
    bh = Bulkhead(1, timeout=0.01, name="api")
    async with bh:
        with pytest.raises(BulkheadFull):
            await bh.acquire_async()
    assert [e.name for e in events] == ["bulkhead.rejected"]
    assert events[0].fields["active"] == 1


def test_limit_concurrency_under_classmethod() -> None:
    class Pool:
        @classmethod
        @limit_concurrency(1)
        def make(cls, x: int) -> tuple[type, int]:
            return cls, x

    assert Pool.make(2) == (Pool, 2)
    assert Pool().make(3) == (Pool, 3)
    assert Pool.make.bulkhead.max_concurrent == 1
