"""Tests for python_wrap.periodic."""

from __future__ import annotations

import asyncio
import contextvars
import itertools
import logging
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from python_wrap import _core
from python_wrap.periodic import AsyncPeriodic, Periodic, _delay_until, _next_tick, periodic

periodic_module = sys.modules["python_wrap.periodic"]

TICK = 0.005


def wait_for(predicate: Any, timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.001)


def periodic_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("python-wrap-periodic")]


@pytest.fixture(autouse=True)
def no_leaks() -> Iterator[None]:
    yield
    wait_for(lambda: not periodic_threads())


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize(
    "kwargs",
    [
        {"interval": 0},
        {"interval": -1},
        {"interval": True},
        {"interval": 1, "jitter": 1.0},
        {"interval": 1, "jitter": -0.1},
        {"interval": 1, "initial_delay": -1},
        {"interval": 1, "max_runs": 0},
        {"interval": 1, "on_error": "explode"},
    ],
)
def test_invalid_arguments(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        periodic(**kwargs)


def test_rejects_non_callable_and_unbound_method() -> None:
    with pytest.raises(TypeError):
        periodic(1)(5)  # type: ignore[call-overload]

    with pytest.raises(TypeError, match="bound method"):

        class C:
            @periodic(1)
            def refresh(self) -> None: ...


def test_bound_method_works() -> None:
    class C:
        def __init__(self) -> None:
            self.n = 0

        def refresh(self, step: int) -> None:
            self.n += step

    c = C()
    task = periodic(TICK, max_runs=3)(c.refresh)
    task.start(2)
    wait_for(lambda: not task.running)
    assert c.n == 6


# ------------------------------------------------------------------ sync


def test_direct_call_and_metadata() -> None:
    @periodic(1)
    def add(a: int, b: int) -> int:
        """Doc."""
        return a + b

    assert isinstance(add, Periodic)
    assert add(1, 2) == 3
    assert add.__name__ == "add"
    assert add.__doc__ == "Doc."
    assert not add.running
    assert add.runs == 0
    assert add.last_error is None
    assert add.stop() is True  # never started: harmless


def test_runs_repeatedly_with_args_and_stops() -> None:
    seen: list[tuple[int, str]] = []

    @periodic(TICK)
    def work(n: int, *, tag: str) -> None:
        seen.append((n, tag))

    assert work.start(1, tag="x") is work
    assert work.running
    wait_for(lambda: work.runs >= 3)
    assert work.stop() is True
    assert not work.running
    count = work.runs
    time.sleep(TICK * 4)
    assert work.runs == count
    assert set(seen) == {(1, "x")}
    assert work.stop() is True  # idempotent


def test_double_start_rejected_and_restart_resets() -> None:
    @periodic(TICK, max_runs=2)
    def work() -> None: ...

    work.start()
    with pytest.raises(RuntimeError, match="already running"):
        work.start()
    wait_for(lambda: not work.running)
    assert work.runs == 2
    work.start()
    wait_for(lambda: not work.running)
    assert work.runs == 2


def test_max_runs_ends_by_itself() -> None:
    @periodic(TICK, max_runs=3)
    def work() -> None: ...

    work.start()
    wait_for(lambda: not work.running)
    assert work.runs == 3
    assert work.stop() is True


def test_initial_delay_and_stop_during_delay() -> None:
    calls: list[int] = []

    @periodic(TICK, initial_delay=30)
    def work() -> None:
        calls.append(1)

    work.start()
    time.sleep(0.02)
    assert work.runs == 0
    started = time.monotonic()
    assert work.stop() is True
    assert time.monotonic() - started < 1
    assert calls == []


def test_context_manager_stops_and_joins() -> None:
    @periodic(TICK)
    def work() -> None: ...

    with work.running_in_background() as inner:
        assert inner is work
        assert work.running
        wait_for(lambda: work.runs >= 1)
    assert not work.running
    assert periodic_threads() == []


def test_context_manager_stops_on_exception() -> None:
    @periodic(TICK)
    def work() -> None: ...

    with pytest.raises(KeyError), work.running_in_background():
        raise KeyError("x")
    assert not work.running


def test_thread_is_daemon_and_named() -> None:
    seen: list[threading.Thread] = []

    @periodic(TICK, max_runs=1, name="heartbeat")
    def work() -> None:
        seen.append(threading.current_thread())

    work.start()
    wait_for(lambda: seen)
    assert seen[0].daemon
    assert seen[0].name == "python-wrap-periodic:heartbeat"


def test_stop_times_out_while_run_in_progress() -> None:
    release = threading.Event()
    entered = threading.Event()

    @periodic(TICK)
    def slow() -> None:
        entered.set()
        release.wait(5)

    slow.start()
    entered.wait(2)
    assert slow.stop(timeout=0.01) is False
    assert slow.running
    release.set()
    assert slow.stop() is True


def test_stop_from_inside_function_does_not_deadlock() -> None:
    @periodic(TICK)
    def work() -> None:
        work.stop()

    work.start()
    wait_for(lambda: not work.running)
    assert work.runs == 1


def test_contextvars_are_copied() -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("var", default="unset")
    seen: list[str] = []

    @periodic(TICK, max_runs=2)
    def work() -> None:
        seen.append(var.get())

    var.set("caller")
    work.start()
    wait_for(lambda: not work.running)
    assert seen == ["caller", "caller"]


# ------------------------------------------------------------------ errors


def boom_every_time() -> Any:
    @periodic(TICK, max_runs=3, on_error="log")
    def work() -> None:
        raise ValueError("bad")

    return work


def test_on_error_log_continues(caplog: pytest.LogCaptureFixture) -> None:
    work = boom_every_time()
    with caplog.at_level(logging.ERROR, logger="python_wrap"):
        work.start()
        wait_for(lambda: not work.running)
    assert work.runs == 3
    assert isinstance(work.last_error, ValueError)
    assert sum("failed" in r.getMessage() for r in caplog.records) == 3
    assert work.stop() is True  # nothing to re-raise in "log" mode


def test_on_error_stop_ends_loop_quietly(caplog: pytest.LogCaptureFixture) -> None:
    @periodic(TICK, on_error="stop")
    def work() -> None:
        raise ValueError("bad")

    with caplog.at_level(logging.ERROR, logger="python_wrap"):
        work.start()
        wait_for(lambda: not work.running)
    assert work.runs == 1
    assert isinstance(work.last_error, ValueError)
    assert work.stop() is True
    assert any("stopping" in r.getMessage() for r in caplog.records)


def test_on_error_raise_surfaces_from_stop_once() -> None:
    @periodic(TICK, on_error="raise")
    def work() -> None:
        raise ValueError("bad")

    work.start()
    wait_for(lambda: not work.running)
    with pytest.raises(ValueError, match="bad"):
        work.stop()
    assert work.stop() is True
    assert isinstance(work.last_error, ValueError)


def test_on_error_raise_via_context_manager() -> None:
    @periodic(TICK, on_error="raise")
    def work() -> None:
        raise ValueError("bad")

    with pytest.raises(ValueError, match="bad"), work.running_in_background():
        wait_for(lambda: not work.running)


def test_custom_logger_name(caplog: pytest.LogCaptureFixture) -> None:
    @periodic(TICK, max_runs=1, logger="mine", level=logging.WARNING)
    def work() -> None:
        raise ValueError("bad")

    with caplog.at_level(logging.WARNING, logger="mine"):
        work.start()
        wait_for(lambda: not work.running)
    assert [r.name for r in caplog.records] == ["mine"]
    assert caplog.records[0].levelno == logging.WARNING


# ------------------------------------------------------------------ events


def test_events() -> None:
    events: list[_core.Event] = []
    _core.listeners.append(events.append)
    try:

        @periodic(TICK, max_runs=2, name="job")
        def ok() -> None: ...

        @periodic(TICK, max_runs=1, name="bad")
        def bad() -> None:
            raise ValueError("x")

        ok.start()
        bad.start()
        wait_for(lambda: not ok.running and not bad.running)
    finally:
        _core.listeners.remove(events.append)
    runs = [e for e in events if e.name == "periodic.run"]
    assert [e.fields["run"] for e in runs] == [1, 2]
    assert all(e.function == "job" and e.fields["seconds"] >= 0 for e in runs)
    (failed,) = [e for e in events if e.name == "periodic.failed"]
    assert failed.function == "bad"
    assert isinstance(failed.fields["exception"], ValueError)


# ------------------------------------------------------------------ schedule maths


def test_next_tick_has_no_drift_and_skips_missed() -> None:
    # On time: simply due + interval, however late within the interval.
    assert _next_tick(10.0, 10.4, 1.0) == 11.0
    # Run took 2.5 intervals: ticks 11 and 12 are skipped, resume at 13.
    assert _next_tick(10.0, 12.5, 1.0) == 13.0
    # Exactly on a tick counts as missed.
    assert _next_tick(10.0, 12.0, 1.0) == 13.0
    # Many consecutive slow runs never move the grid off first + n * interval.
    due = 100.0
    for now in (100.9, 102.2, 102.9, 105.5, 106.0):
        due = _next_tick(due, now, 1.0)
        assert (due - 100.0) % 1.0 == pytest.approx(0.0, abs=1e-9)
        assert due > now


def test_jitter_bounds_and_scale(monkeypatch: pytest.MonkeyPatch) -> None:
    for _ in range(500):
        delay = _delay_until(10.0, 0.0, 2.0, 0.25)
        assert 9.5 <= delay <= 10.5
    assert _delay_until(10.0, 0.0, 2.0, 0.0) == 10.0
    assert _delay_until(1.0, 5.0, 2.0, 0.0) == 0.0  # never negative
    seen: list[tuple[float, float]] = []
    monkeypatch.setattr(periodic_module, "_uniform", lambda a, b: seen.append((a, b)) or 0.0)
    _delay_until(10.0, 0.0, 4.0, 0.1)
    assert seen == [(-0.1, 0.1)]


def test_loop_real_time_start_to_start_without_drift() -> None:
    starts: list[float] = []
    interval = 0.04

    @periodic(interval, max_runs=5)
    def work() -> None:
        starts.append(time.monotonic())
        time.sleep(0.015)  # work time must not stretch the period

    work.start()
    wait_for(lambda: not work.running)
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert len(gaps) == 4
    # Drift would add the 0.015 s of work to every gap (4 * 0.055 total).
    assert starts[-1] - starts[0] < 4 * interval + 0.03


def test_overrun_skips_missed_ticks_instead_of_bursting() -> None:
    starts: list[float] = []
    interval = 0.02

    @periodic(interval, max_runs=3)
    def work() -> None:
        starts.append(time.monotonic())
        if len(starts) == 1:
            time.sleep(0.07)  # misses ~3 ticks

    work.start()
    wait_for(lambda: not work.running)
    # A burst would start run 2 immediately after run 1 ends; skipping waits for the grid.
    assert starts[1] - starts[0] >= 0.07


# ------------------------------------------------------------------ async


async def spin_until(predicate: Any, timeout: float = 2.0) -> None:
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.001)


def test_async_start_outside_loop_is_clear_error() -> None:
    @periodic(1)
    async def work() -> None: ...

    with pytest.raises(RuntimeError, match="running event loop"):
        work.start()


async def test_async_run_stop_and_direct_call() -> None:
    seen: list[int] = []

    @periodic(TICK)
    async def work(n: int) -> int:
        seen.append(n)
        return n * 2

    assert isinstance(work, AsyncPeriodic)
    assert await work(4) == 8
    assert work.start(7) is work
    assert work.running
    with pytest.raises(RuntimeError, match="already running"):
        work.start(7)
    await spin_until(lambda: work.runs >= 3)
    assert await work.stop() is True
    assert not work.running
    count = work.runs
    await asyncio.sleep(TICK * 4)
    assert work.runs == count
    assert 7 in seen
    assert await work.stop() is True
    others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert others == []


async def test_async_marked_as_coroutine_function_when_supported() -> None:
    import inspect

    @periodic(1)
    async def work() -> None: ...

    if hasattr(inspect, "markcoroutinefunction"):
        assert inspect.iscoroutinefunction(work)


async def test_async_max_runs_and_initial_delay() -> None:
    @periodic(TICK, max_runs=2, initial_delay=0.02)
    async def work() -> None: ...

    work.start()
    await asyncio.sleep(0.005)
    assert work.runs == 0
    await spin_until(lambda: not work.running)
    assert work.runs == 2
    work.start()  # restart resets
    await spin_until(lambda: not work.running)
    assert work.runs == 2


async def test_async_context_manager() -> None:
    @periodic(TICK)
    async def work() -> None: ...

    async with work.running_in_background() as inner:
        assert inner is work
        await spin_until(lambda: work.runs >= 1)
    assert not work.running


async def test_async_stop_cancels_run_in_progress() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    @periodic(TICK)
    async def work() -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    work.start()
    await started.wait()
    await work.stop()
    assert cancelled.is_set()
    assert not work.running


async def test_async_stop_from_inside_function() -> None:
    @periodic(TICK)
    async def work() -> None:
        await work.stop()

    work.start()
    await spin_until(lambda: not work.running)
    assert work.runs == 1


async def test_async_error_modes(caplog: pytest.LogCaptureFixture) -> None:
    @periodic(TICK, max_runs=3)
    async def logs() -> None:
        raise ValueError("bad")

    @periodic(TICK, on_error="stop")
    async def stops() -> None:
        raise ValueError("bad")

    @periodic(TICK, on_error="raise")
    async def raises() -> None:
        raise KeyError("bad")

    with caplog.at_level(logging.ERROR, logger="python_wrap"):
        logs.start()
        stops.start()
        raises.start()
        await spin_until(lambda: not (logs.running or stops.running or raises.running))
    assert logs.runs == 3
    assert stops.runs == 1
    assert isinstance(stops.last_error, ValueError)
    assert await logs.stop() and await stops.stop()
    with pytest.raises(KeyError):
        await raises.stop()
    assert await raises.stop() is True


async def test_async_raise_via_context_manager() -> None:
    @periodic(TICK, on_error="raise")
    async def work() -> None:
        raise ValueError("bad")

    with pytest.raises(ValueError):
        async with work.running_in_background():
            await spin_until(lambda: not work.running)


async def test_async_contextvars_and_events() -> None:
    var: contextvars.ContextVar[str] = contextvars.ContextVar("var", default="unset")
    seen: list[str] = []
    events: list[str] = []
    _core.listeners.append(lambda e: events.append(e.name))
    listener = _core.listeners[-1]

    @periodic(TICK, max_runs=1)
    async def work() -> None:
        seen.append(var.get())

    try:
        var.set("caller")
        work.start()
        await spin_until(lambda: not work.running)
    finally:
        _core.listeners.remove(listener)
    assert seen == ["caller"]
    assert events == ["periodic.run"]


async def test_async_jitter_runs() -> None:
    @periodic(TICK, jitter=0.5, max_runs=3)
    async def work() -> None: ...

    work.start()
    await spin_until(lambda: not work.running)
    assert work.runs == 3


def test_fast_mode_does_not_shorten_schedule() -> None:
    from python_wrap.testing import fast_mode

    @periodic(0.2, initial_delay=0.2)
    def work() -> None: ...

    with fast_mode():
        work.start()
        time.sleep(0.05)
        assert work.runs == 0
        work.stop()
