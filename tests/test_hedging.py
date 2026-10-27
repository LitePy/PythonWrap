from __future__ import annotations

import asyncio
import contextvars
import gc
import inspect
import threading

import pytest

from python_wrap.hedging import hedge


def only_current_task():
    return asyncio.all_tasks() == {asyncio.current_task()}


def hedge_threads():
    return [t for t in threading.enumerate() if t.name.startswith("hedge-")]


@pytest.mark.parametrize(
    "kwargs", [{"delay": -1}, {"delay": 1, "max_extra": -1}, {"delay": 1, "max_extra": True}]
)
def test_validates_options(kwargs):
    with pytest.raises(ValueError):
        hedge(**kwargs)


# --------------------------------------------------------------------------- async


async def test_async_fast_call_runs_once():
    calls = []

    @hedge(10)
    async def fetch(x: int) -> int:
        calls.append(x)
        return x * 2

    assert inspect.iscoroutinefunction(fetch)
    assert fetch.__name__ == "fetch"
    assert await fetch(4) == 8
    assert calls == [4]
    assert only_current_task()


async def test_async_slow_call_is_hedged_and_loser_cancelled(fake_clock):
    cancelled = []
    calls = []

    @hedge(0.2)
    async def fetch() -> str:
        n = len(calls)
        calls.append(n)
        if n == 0:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(n)
                raise
        return f"attempt{n}"

    assert await fetch() == "attempt1"
    assert cancelled == [0]
    assert fake_clock.sleeps == [0.2]
    assert only_current_task()


async def test_async_failure_starts_the_next_attempt_immediately():
    calls = []

    @hedge(10, max_extra=2, on=OSError)
    async def fetch() -> int:
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("flaky")
        return len(calls)

    assert await fetch() == 3  # no 10s waits: each failure triggers the next hedge
    assert only_current_task()


async def test_async_all_fail_raises_the_last_exception(fake_clock):
    calls = []

    @hedge(0.1, max_extra=2)
    async def fetch() -> None:
        calls.append(1)
        raise ValueError(len(calls))

    with pytest.raises(ValueError) as info:
        await fetch()
    assert info.value.args == (3,)
    assert len(calls) == 3


async def test_async_non_matching_error_is_raised_at_once(fake_clock):
    cancelled = []

    @hedge(0.1, on=OSError)
    async def fetch(n: list[int]) -> None:
        n.append(1)
        if len(n) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append(1)
                raise
        raise KeyError("bug")

    with pytest.raises(KeyError):
        await fetch([])
    assert cancelled == [1]
    assert only_current_task()


async def test_async_max_extra_caps_the_attempts(fake_clock):
    started = []
    gate = asyncio.Event()

    @hedge(0.1, max_extra=2)
    async def fetch() -> int:
        started.append(1)
        n = len(started)
        if n == 3:
            gate.set()
        await gate.wait()
        return n

    assert await fetch() in (1, 2, 3)
    assert len(started) == 3
    assert only_current_task()


async def test_async_max_extra_zero_never_hedges(fake_clock):
    calls = []

    @hedge(0.1, max_extra=0)
    async def fetch() -> int:
        calls.append(1)
        await asyncio.sleep(0)
        return 1

    assert await fetch() == 1
    assert calls == [1]
    assert fake_clock.sleeps == []


async def test_async_cancelling_the_caller_cancels_every_attempt(fake_clock):
    started = []
    cancelled = []

    @hedge(0.1, max_extra=1)
    async def fetch() -> None:
        started.append(1)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    task = asyncio.create_task(fetch())
    while len(started) < 2:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == [1, 1]
    assert only_current_task()


async def test_async_loser_error_while_cancelling_is_swallowed(fake_clock):
    calls = []

    @hedge(0.1)
    async def fetch() -> str:
        calls.append(1)
        if len(calls) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("cleanup failed") from None
        return "ok"

    assert await fetch() == "ok"
    gc.collect()  # an unretrieved task exception would be reported (filterwarnings=error)


async def test_async_attempt_cancelled_from_inside_propagates():
    @hedge(10)
    async def fetch() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await fetch()


async def test_async_methods_and_context(fake_clock):
    var = contextvars.ContextVar("var", default="unset")

    class Client:
        @hedge(0.1)
        async def get(self, path: str) -> str:
            return f"{var.get()}:{path}"

    var.set("ctx")
    assert await Client().get("/a") == "ctx:/a"


# --------------------------------------------------------------------------- sync


def test_sync_fast_call_runs_once():
    calls = []

    @hedge(10)
    def fetch(x: int) -> int:
        calls.append(threading.current_thread().name)
        return x + 1

    assert fetch(1) == 2
    assert fetch.__name__ == "fetch"
    assert calls == ["hedge-test_sync_fast_call_runs_once.<locals>.fetch"]


def test_sync_slow_call_is_hedged_and_loser_finishes_in_background():
    release = threading.Event()
    calls = []

    @hedge(0.01)
    def fetch() -> str:
        calls.append(1)
        if len(calls) == 1:
            release.wait(5)
            return "slow"
        return "fast"

    assert fetch() == "fast"
    assert len(calls) == 2
    release.set()
    for t in hedge_threads():
        t.join(5)
    assert hedge_threads() == []


def test_sync_failure_starts_the_next_attempt_immediately():
    calls = []

    @hedge(10, max_extra=2, on=(OSError, TimeoutError))
    def fetch() -> int:
        calls.append(1)
        if len(calls) < 3:
            raise TimeoutError
        return len(calls)

    assert fetch() == 3


def test_sync_all_fail_raises_the_last_exception():
    calls = []
    lock = threading.Lock()

    @hedge(10, max_extra=1)
    def fetch() -> None:
        with lock:
            calls.append(1)
            n = len(calls)
        raise ValueError(n)

    with pytest.raises(ValueError) as info:
        fetch()
    assert info.value.args == (2,)


def test_sync_non_matching_error_is_raised_at_once():
    @hedge(10, on=OSError)
    def fetch() -> None:
        raise KeyError("bug")

    with pytest.raises(KeyError):
        fetch()


def test_sync_max_extra_zero_and_methods():
    var = contextvars.ContextVar("var", default="unset")

    class Client:
        @hedge(0.01, max_extra=0)
        def get(self, path: str) -> str:
            return f"{var.get()}:{path}"

    var.set("ctx")
    assert Client().get("/a") == "ctx:/a"


def test_sync_max_extra_caps_the_attempts():
    gate = threading.Event()
    started = []
    lock = threading.Lock()

    @hedge(0.005, max_extra=2)
    def fetch() -> int:
        with lock:
            started.append(1)
            n = len(started)
        if n == 3:
            gate.set()
        gate.wait(5)
        return n

    assert fetch() in (1, 2, 3)
    assert len(started) == 3
    for t in hedge_threads():
        t.join(5)
