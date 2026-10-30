"""Application job delay contracts across both retry interfaces."""

from __future__ import annotations

import asyncio
import random
from typing import Any

import pytest

from conftest import FakeClock
from python_wrap import deadline, retry, retrying


@pytest.mark.parametrize("inline", [False, True])
def test_job_schedule_repeats_last_and_preserves_exception(
    fake_clock: FakeClock, inline: bool
) -> None:
    error = RuntimeError("job failed")
    attempts = []

    def job() -> None:
        attempts.append(1)
        raise error

    with pytest.raises(RuntimeError) as caught:
        if inline:
            for attempt in retrying(max_attempts=5, delays=[5, 30, 120]):
                with attempt:
                    job()
        else:
            retry(job, max_attempts=5, delays=[5, 30, 120])()
    assert caught.value is error
    assert len(attempts) == 5
    assert fake_clock.sleeps == [5, 30, 120, 120]


@pytest.mark.parametrize("inline", [False, True])
async def test_async_schedule(fake_clock: FakeClock, inline: bool) -> None:
    calls = 0

    async def job() -> str:
        nonlocal calls
        calls += 1
        if calls < 4:
            raise ConnectionError
        return "done"

    if inline:
        async for attempt in retrying(max_attempts=4, delays=[0, 2]):
            async with attempt:
                assert await job() == "done"
    else:
        assert await retry(job, max_attempts=4, delays=[0, 2])() == "done"
    assert calls == 4
    assert fake_clock.sleeps == [0, 2, 2]


def test_schedule_snapshot_and_hint_precedence(fake_clock: FakeClock) -> None:
    waits = [5.0, 30.0]
    values = iter([1, 2, 3, 4])
    wrapped = retry(
        lambda: next(values),
        max_attempts=4,
        delays=waits,
        delay=99,
        backoff=9,
        max_delay=10,
        retry_if_result=lambda value: value < 4,
        delay_from=lambda value: 2 if value == 1 else None,
    )
    waits[:] = [99]
    assert wrapped() == 4
    assert fake_clock.sleeps == [2, 10, 10]
    # Each invocation starts at the first schedule entry.
    values = iter([2, 4])
    assert wrapped() == 4
    assert fake_clock.sleeps[-1] == 5


@pytest.mark.parametrize("use_deadline", [False, True])
def test_schedule_respects_time_budget(fake_clock: FakeClock, use_deadline: bool) -> None:
    def job() -> None:
        raise ConnectionError

    wrapped = retry(job, delays=[5, 30], max_time=None if use_deadline else 10)
    with pytest.raises(ConnectionError), deadline(10 if use_deadline else 100):
        wrapped()
    assert fake_clock.sleeps == [5]


def test_schedule_jitter_after_cap(fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "uniform", lambda low, high: high)
    values = iter([False, True])
    assert retry(
        lambda: next(values), delays=[5], max_delay=2, jitter=1, retry_if_result=lambda x: not x
    )()
    assert fake_clock.sleeps == [3]


@pytest.mark.parametrize("factory", [retry, retrying])
@pytest.mark.parametrize("waits", [[], [-1], [float("nan")], [float("inf")], [True], "5", ["5"]])
def test_invalid_schedule_fails_eagerly(factory: Any, waits: Any) -> None:
    with pytest.raises(ValueError, match="delays"):
        factory(delays=waits)


async def test_cancel_during_scheduled_sleep() -> None:
    failed = asyncio.Event()

    @retry(delays=[3600])
    async def job() -> None:
        failed.set()
        raise ConnectionError

    task = asyncio.create_task(job())
    await failed.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
