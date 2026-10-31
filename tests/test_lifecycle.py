"""Tests for python_wrap.lifecycle."""

from __future__ import annotations

import asyncio
import atexit
import os
import signal
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from python_wrap import events
from python_wrap.adaptive import AdaptiveLimiter
from python_wrap.batching import batch_calls
from python_wrap.buffering import buffered
from python_wrap.bulkhead import Bulkhead
from python_wrap.concurrency import debounce
from python_wrap.lifecycle import ShutdownError, ShutdownManager, shutdown
from python_wrap.periodic import periodic

posix_signals = pytest.mark.skipif(
    not hasattr(signal, "SIGUSR1") or os.name == "nt", reason="needs POSIX signals"
)


@pytest.fixture
def manager() -> ShutdownManager:
    return ShutdownManager()


def test_orders_by_priority_then_registration(manager: ShutdownManager) -> None:
    order: list[str] = []
    manager.register(lambda: order.append("c"), name="c", priority=5)
    manager.register(lambda: order.append("a"), name="a", priority=-1)
    manager.register(lambda: order.append("b1"), name="b1", priority=0)
    manager.register(lambda: order.append("b2"), name="b2", priority=0)
    assert manager.names == ["a", "b1", "b2", "c"]
    report = manager.drain()
    assert order == ["a", "b1", "b2", "c"]
    assert [i.name for i in report.items] == order
    assert report.ok


def test_register_returns_target_and_rejects_non_callables(manager: ShutdownManager) -> None:
    def f() -> None: ...

    assert manager.register(f) is f
    with pytest.raises(TypeError):
        manager.register(42)


def test_errors_are_recorded_and_do_not_stop_the_rest(manager: ShutdownManager) -> None:
    ran: list[str] = []

    def boom() -> None:
        raise ValueError("nope")

    manager.register(boom, name="boom", priority=0)
    manager.register(lambda: ran.append("after"), name="after", priority=1)
    report = manager.drain()
    assert ran == ["after"]
    assert not report.ok
    assert [i.name for i in report.failed] == ["boom"]
    assert isinstance(report.failed[0].error, ValueError)


def test_strict_raises_with_report(manager: ShutdownManager) -> None:
    manager.register(lambda: 1 / 0, name="bad")
    with pytest.raises(ShutdownError) as info:
        manager.drain(strict=True)
    assert info.value.report.failed[0].name == "bad"


def test_slow_callable_times_out_and_budget_skips_the_rest(manager: ShutdownManager) -> None:
    release = threading.Event()
    manager.register(lambda: release.wait(5), name="slow", priority=0)
    manager.register(lambda: None, name="skipped", priority=1)
    started = time.monotonic()
    report = manager.drain(timeout=0.1)
    release.set()
    assert time.monotonic() - started < 2
    assert [(i.name, i.status) for i in report.items] == [
        ("slow", "timeout"),
        ("skipped", "timeout"),
    ]
    assert [i.name for i in report.timed_out] == ["slow", "skipped"]


def test_async_callable_in_sync_drain(manager: ShutdownManager) -> None:
    done: list[str] = []

    async def close_pool() -> None:
        await asyncio.sleep(0.01)
        done.append("pool")

    manager.register(close_pool, name="pool")
    assert manager.drain().ok
    assert done == ["pool"]


async def test_adrain_mixes_sync_and_async_callables(manager: ShutdownManager) -> None:
    done: list[str] = []

    async def a() -> None:
        await asyncio.sleep(0.01)
        done.append("a")

    manager.register(lambda: done.append("s"), name="s", priority=0)
    manager.register(a, name="a", priority=1)
    report = await manager.adrain()
    assert report.ok and done == ["s", "a"]
    assert (await manager.adrain()) is report


async def test_adrain_timeout_and_error(manager: ShutdownManager) -> None:
    async def hang() -> None:
        await asyncio.sleep(10)

    def boom() -> None:
        raise RuntimeError("x")

    manager.register(boom, name="boom", priority=0)
    manager.register(hang, name="hang", priority=1)
    report = await manager.adrain(timeout=0.1)
    assert [(i.name, i.status) for i in report.items] == [("boom", "error"), ("hang", "timeout")]
    with pytest.raises(ShutdownError):
        await manager.adrain(strict=True)


def test_idempotent(manager: ShutdownManager) -> None:
    calls: list[int] = []
    manager.register(lambda: calls.append(1))
    first = manager.drain()
    assert manager.drain() is first
    assert calls == [1]
    assert manager.shutting_down and manager.report is first
    with pytest.raises(RuntimeError):
        manager.register(lambda: None)
    manager.reset()
    assert not manager.shutting_down
    manager.register(lambda: calls.append(2))
    manager.drain()
    assert calls == [1, 2]


def test_unregister(manager: ShutdownManager) -> None:
    def f() -> None: ...

    manager.register(f, name="f")
    manager.register(lambda: None, name="g")
    assert manager.unregister(f) and manager.unregister("g")
    assert not manager.unregister("g")
    assert manager.drain().items == ()


def test_concurrent_drain_waits_for_the_first(manager: ShutdownManager) -> None:
    gate = threading.Event()
    manager.register(lambda: gate.wait(5), name="gate")
    first: list[Any] = []
    t = threading.Thread(target=lambda: first.append(manager.drain()))
    t.start()
    while not manager.shutting_down:
        time.sleep(0.001)
    threading.Timer(0.05, gate.set).start()
    second = manager.drain()
    t.join()
    assert second is first[0]


def test_events(manager: ShutdownManager) -> None:
    seen: list[str] = []
    manager.register(lambda: None, name="x")
    with events.listening(lambda e: seen.append(e.name)):
        manager.drain()
    assert seen == ["lifecycle.drain_started", "lifecycle.item_done", "lifecycle.drain_finished"]


# ------------------------------------------------------------- python-wrap objects


def test_periodic_is_stopped(manager: ShutdownManager) -> None:
    ticks: list[int] = []

    @periodic(0.005)
    def tick() -> None:
        ticks.append(1)

    tick.start()
    while not ticks:
        time.sleep(0.005)
    manager.register(tick, name="tick")
    report = manager.drain()
    assert report.ok and not tick.running


def test_buffered_is_flushed_and_closed(manager: ShutdownManager) -> None:
    written: list[Any] = []

    @buffered(max_size=100, flush_interval=60)
    def sink(batch: list[Any]) -> None:
        written.extend(batch)

    for i in range(5):
        sink.submit(i)
    manager.register(sink, name="sink")
    assert manager.drain().ok
    assert written == [0, 1, 2, 3, 4]
    assert sink.stats().queued == 0
    assert not sink.submit(99)  # closed: no longer accepting


def test_batcher_and_debouncer_are_flushed(manager: ShutdownManager) -> None:
    seen: list[Any] = []

    @debounce(60)
    def save(x: int) -> None:
        seen.append(x)

    @batch_calls(max_size=10, max_wait=60)
    def load(items: list[int]) -> list[int]:
        seen.append(("batch", tuple(items)))
        return items

    save(7)
    results: list[int] = []
    t = threading.Thread(target=lambda: results.append(load(1)))
    t.start()
    while load.pending < 1:
        time.sleep(0.001)
    manager.register(save, name="save", priority=0)
    manager.register(load, name="load", priority=1)
    assert manager.drain(timeout=5).ok
    t.join(5)
    assert results == [1]
    assert seen == [7, ("batch", (1,))]


def test_bulkhead_drain_waits_for_active_calls(manager: ShutdownManager) -> None:
    gate = Bulkhead(2)
    release = threading.Event()

    def work() -> None:
        with gate:
            release.wait(5)

    t = threading.Thread(target=work)
    t.start()
    while gate.active == 0:
        time.sleep(0.001)
    manager.register(gate, name="gate")
    threading.Timer(0.05, release.set).start()
    report = manager.drain(timeout=5)
    t.join()
    assert report.ok and gate.active == 0


def test_bulkhead_still_busy_times_out(manager: ShutdownManager) -> None:
    gate = Bulkhead(1)
    gate.acquire()
    manager.register(gate, name="gate")
    report = manager.drain(timeout=0.05)
    assert report.items[0].status == "timeout"
    gate.release()


def test_adaptive_limiter_idle_wait(manager: ShutdownManager) -> None:
    limiter = AdaptiveLimiter(initial=2)
    permit = limiter.acquire()
    manager.register(limiter, name="limiter")
    threading.Timer(0.05, permit.release).start()
    assert manager.drain(timeout=5).ok


async def test_adrain_handles_async_periodic_and_sync_objects(manager: ShutdownManager) -> None:
    ticks: list[int] = []

    @periodic(0.005)
    async def tick() -> None:
        ticks.append(1)

    written: list[Any] = []

    @buffered(max_size=100, flush_interval=60)
    def sink(batch: list[Any]) -> None:
        written.extend(batch)

    tick.start()
    sink.submit("x")
    await asyncio.sleep(0.03)
    manager.register(tick, name="tick", priority=0)
    manager.register(sink, name="sink", priority=1)
    report = await manager.adrain(timeout=5)
    assert report.ok and not tick.running and written == ["x"]


async def test_adrain_async_buffered(manager: ShutdownManager) -> None:
    written: list[Any] = []

    @buffered(max_size=100, flush_interval=60)
    async def sink(batch: list[Any]) -> None:
        written.extend(batch)

    await asyncio.sleep(0)
    assert await sink.submit("a")
    manager.register(sink, name="sink")
    report = await manager.adrain(timeout=5)
    assert report.ok and written == ["a"]


async def test_sync_drain_reports_loop_bound_items(manager: ShutdownManager) -> None:
    @periodic(0.01)
    async def tick() -> None: ...

    tick.start()
    manager.register(tick, name="tick")
    report = manager.drain()
    assert report.items[0].status == "error"
    assert "adrain" in str(report.items[0].error)
    await tick.stop()


# ------------------------------------------------------------------------- atexit


def test_install_atexit_registers_once(
    manager: ShutdownManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    registered: list[Any] = []
    unregistered: list[Any] = []
    monkeypatch.setattr(atexit, "register", lambda fn, *a, **k: registered.append(fn) or fn)
    monkeypatch.setattr(atexit, "unregister", lambda fn: unregistered.append(fn))
    ran: list[int] = []
    manager.register(lambda: ran.append(1))
    manager.install_atexit(timeout=5)
    manager.install_atexit(timeout=5)
    assert len(registered) == 1
    registered[0]()
    assert ran == [1]
    manager.uninstall_atexit()
    assert unregistered == [registered[0]]


# ------------------------------------------------------------------------- signals


@pytest.fixture
def restore_usr1() -> Iterator[None]:
    previous = signal.getsignal(signal.SIGUSR1)
    yield
    signal.signal(signal.SIGUSR1, previous)


@posix_signals
def test_signal_handler_drains_chains_and_restores(
    manager: ShutdownManager, restore_usr1: None
) -> None:
    calls: list[str] = []

    def old(signum: int, frame: Any) -> None:
        calls.append("old")

    signal.signal(signal.SIGUSR1, old)
    manager.register(lambda: calls.append("drain"))
    assert manager.install_signal_handlers([signal.SIGUSR1], exit=False) == [signal.SIGUSR1]
    assert manager.install_signal_handlers([signal.SIGUSR1], exit=False) == []  # already
    assert signal.getsignal(signal.SIGUSR1) is not old
    os.kill(os.getpid(), signal.SIGUSR1)
    for _ in range(100):
        if "old" in calls:
            break
        time.sleep(0.01)
    assert calls == ["drain", "old"]
    assert manager.restore_signal_handlers() == [signal.SIGUSR1]
    assert signal.getsignal(signal.SIGUSR1) is old


@posix_signals
def test_signal_handler_without_chain(manager: ShutdownManager, restore_usr1: None) -> None:
    calls: list[str] = []
    signal.signal(signal.SIGUSR1, lambda s, f: calls.append("old"))
    manager.register(lambda: calls.append("drain"))
    manager.install_signal_handlers([signal.SIGUSR1], exit=False, chain=False)
    os.kill(os.getpid(), signal.SIGUSR1)
    for _ in range(100):
        if manager.report is not None:
            break
        time.sleep(0.01)
    assert calls == ["drain"]
    manager.restore_signal_handlers()


@posix_signals
def test_signal_handler_exits(manager: ShutdownManager, restore_usr1: None) -> None:
    manager.install_signal_handlers([signal.SIGUSR1])
    with pytest.raises(SystemExit) as info:
        os.kill(os.getpid(), signal.SIGUSR1)
        for _ in range(100):
            time.sleep(0.01)
    assert info.value.code == 128 + signal.SIGUSR1
    assert manager.report is not None
    manager.restore_signal_handlers()


@posix_signals
def test_restore_leaves_foreign_handlers_alone(
    manager: ShutdownManager, restore_usr1: None
) -> None:
    manager.install_signal_handlers([signal.SIGUSR1], exit=False)

    def foreign(signum: int, frame: Any) -> None: ...

    signal.signal(signal.SIGUSR1, foreign)
    assert manager.restore_signal_handlers() == []
    assert signal.getsignal(signal.SIGUSR1) is foreign


@posix_signals
async def test_signal_inside_running_loop_schedules_adrain(
    manager: ShutdownManager, restore_usr1: None
) -> None:
    done: list[str] = []

    async def close() -> None:
        done.append("closed")

    manager.register(close)
    manager.install_signal_handlers([signal.SIGUSR1], exit=False)
    os.kill(os.getpid(), signal.SIGUSR1)
    for _ in range(100):
        await asyncio.sleep(0.01)
        if manager.report is not None:
            break
    assert done == ["closed"]
    manager.restore_signal_handlers()


def test_signal_install_requires_main_thread(manager: ShutdownManager) -> None:
    errors: list[BaseException] = []

    def target() -> None:
        try:
            manager.install_signal_handlers([signal.SIGTERM])
        except RuntimeError as exc:
            errors.append(exc)

    t = threading.Thread(target=target)
    t.start()
    t.join()
    assert len(errors) == 1


# ---------------------------------------------------------------------------- fork


def test_fork_child_gets_an_empty_registry(manager: ShutdownManager) -> None:
    manager.register(lambda: None, name="x")
    manager.drain()
    manager._reset_after_fork()
    assert manager.names == [] and not manager.shutting_down
    assert manager.drain().items == ()


def test_module_level_manager_exists() -> None:
    assert isinstance(shutdown, ShutdownManager)
