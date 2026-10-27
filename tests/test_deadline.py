from __future__ import annotations

import asyncio
import contextvars
import signal
import time
from collections.abc import Iterator

import pytest

from conftest import FakeClock
from python_wrap import _core
from python_wrap.concurrency import timeout
from python_wrap.deadline import Deadline, check, deadline, remaining
from python_wrap.errors import TimeoutExpired

needs_signals = pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs setitimer")


@pytest.fixture
def events() -> Iterator[list[_core.Event]]:
    seen: list[_core.Event] = []
    _core.listeners.append(seen.append)
    try:
        yield seen
    finally:
        _core.listeners.remove(seen.append)


def test_no_deadline_by_default() -> None:
    assert remaining() is None
    check()  # no-op


def test_validates() -> None:
    with pytest.raises(ValueError):
        deadline(-1)
    with deadline(0):
        assert remaining() == 0.0


def test_remaining_and_check(fake_clock: FakeClock) -> None:
    with deadline(10) as d:
        assert isinstance(d, Deadline)
        assert remaining() == pytest.approx(10)
        assert d.expires_at == pytest.approx(fake_clock.now + 10)
        fake_clock.advance(4)
        assert remaining() == pytest.approx(6)
        check()
        fake_clock.advance(7)
        assert remaining() == 0.0  # never negative
        with pytest.raises(TimeoutExpired) as info:
            check()
        assert info.value.seconds == 10
        assert isinstance(info.value, TimeoutError)
    assert remaining() is None
    assert d.expires_at is None
    assert "Deadline(seconds=10" in repr(d)


def test_nesting_earliest_wins(fake_clock: FakeClock) -> None:
    with deadline(5):
        with deadline(60):
            assert remaining() == pytest.approx(5)  # cannot extend the outer budget
        with deadline(1):
            assert remaining() == pytest.approx(1)
            fake_clock.advance(1)
            with pytest.raises(TimeoutExpired) as info:
                check()
            assert info.value.seconds == 1
        assert remaining() == pytest.approx(4)
    assert remaining() is None


def test_same_instance_reentered(fake_clock: FakeClock) -> None:
    d = deadline(3)
    with d:
        fake_clock.advance(1)
        with d:
            assert remaining() == pytest.approx(2)  # the earlier expiry stays
        assert remaining() == pytest.approx(2)
    assert remaining() is None


def test_async_and_task_propagation(fake_clock: FakeClock) -> None:
    async def child() -> float | None:
        return remaining()

    async def scenario() -> None:
        async with deadline(8):
            assert await asyncio.create_task(child()) == pytest.approx(8)
        assert await asyncio.create_task(child()) is None

    asyncio.run(scenario())


def test_context_is_isolated_between_contexts(fake_clock: FakeClock) -> None:
    ctx = contextvars.copy_context()
    with deadline(2):
        assert ctx.run(remaining) is None


async def test_timeout_decorator_clamps_to_deadline_async(events: list[_core.Event]) -> None:
    @timeout(5)
    async def slow() -> None:
        await asyncio.sleep(1)

    started = time.perf_counter()
    with deadline(0.02), pytest.raises(TimeoutExpired) as info:
        await slow()
    assert time.perf_counter() - started < 0.5
    assert info.value.seconds < 0.03
    assert [e.name for e in events] == ["timeout.expired"]


async def test_timeout_block_clamps_to_deadline_async() -> None:
    with deadline(0.02), pytest.raises(TimeoutExpired) as info:
        async with timeout(5):
            await asyncio.sleep(1)
    assert info.value.seconds < 0.03


@needs_signals
def test_timeout_clamps_to_deadline_sync() -> None:
    @timeout(5)
    def slow() -> None:
        time.sleep(1)

    with deadline(0.02), pytest.raises(TimeoutExpired) as info:
        slow()
    assert info.value.seconds < 0.03


def test_timeout_thread_strategy_clamps_to_deadline() -> None:
    @timeout(5, use_signals=False)
    def slow() -> None:
        time.sleep(0.2)

    with deadline(0.02), pytest.raises(TimeoutExpired) as info:
        slow()
    assert info.value.seconds < 0.03


@needs_signals
def test_timeout_not_started_when_deadline_passed(
    fake_clock: FakeClock, events: list[_core.Event]
) -> None:
    calls: list[int] = []

    @timeout(5)
    def f() -> None:
        calls.append(1)

    with deadline(1):
        fake_clock.advance(1)
        with pytest.raises(TimeoutExpired) as info:
            f()
        assert info.value.seconds == 0.0
        with pytest.raises(TimeoutExpired), timeout(5):
            calls.append(2)
    assert calls == []
    assert [e.name for e in events] == ["timeout.expired", "timeout.expired"]


def test_timeout_longer_deadline_keeps_configured_limit(fake_clock: FakeClock) -> None:
    @timeout(0.5)
    def f() -> int:
        return 1

    with deadline(100):
        assert f() == 1
