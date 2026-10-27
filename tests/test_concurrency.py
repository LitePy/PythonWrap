from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import signal
import threading
import time

import pytest

from python_wrap import concurrency
from python_wrap.concurrency import (
    debounce,
    rate_limit,
    run_in_thread,
    singleflight,
    throttle,
    timeout,
)
from python_wrap.errors import RateLimitExceeded, TimeoutExpired

HAS_SIGNALS = hasattr(signal, "setitimer")
needs_signals = pytest.mark.skipif(not HAS_SIGNALS, reason="needs signal.setitimer")


# --------------------------------------------------------------------------------------
# timeout
# --------------------------------------------------------------------------------------


def test_timeout_validates_seconds():
    with pytest.raises(ValueError):
        timeout(0)


@pytest.mark.parametrize("use_signals", [None, False])
def test_timeout_passes_result_through(use_signals):
    @timeout(1, use_signals=use_signals)
    def add(a, b):
        return a + b

    assert add(1, b=2) == 3
    assert add.__name__ == "add"


@pytest.mark.parametrize("use_signals", [None, False])
def test_timeout_propagates_exceptions(use_signals):
    @timeout(1, use_signals=use_signals)
    def boom():
        raise KeyError("x")

    with pytest.raises(KeyError):
        boom()


@needs_signals
def test_timeout_signal_interrupts_and_restores_handler():
    before = signal.getsignal(signal.SIGALRM)

    @timeout(0.02)
    def slow():
        time.sleep(1)

    started = time.perf_counter()
    with pytest.raises(TimeoutExpired) as info:
        slow()
    assert time.perf_counter() - started < 0.5
    assert isinstance(info.value, TimeoutError)
    assert info.value.seconds == 0.02
    assert signal.getsignal(signal.SIGALRM) is before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


@needs_signals
def test_timeout_nested_inner_fires_and_outer_is_rearmed():
    @timeout(0.02, use_signals=True)
    def inner():
        time.sleep(1)

    @timeout(5, use_signals=True)
    def outer():
        with pytest.raises(TimeoutExpired) as info:
            inner()
        assert info.value.seconds == 0.02
        remaining, _ = signal.getitimer(signal.ITIMER_REAL)
        assert 4 < remaining <= 5
        return "ok"

    assert outer() == "ok"
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


@needs_signals
def test_timeout_nested_outer_shorter_wins():
    @timeout(5, use_signals=True)
    def inner():
        time.sleep(1)

    @timeout(0.02, use_signals=True)
    def outer():
        inner()

    with pytest.raises(TimeoutExpired) as info:
        outer()
    assert info.value.seconds == 0.02
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


@needs_signals
def test_timeout_signals_required_in_main_thread():
    @timeout(1, use_signals=True)
    def f():
        return 1

    assert run_in_thread(f)().exception(timeout=1).__class__ is RuntimeError


def test_timeout_use_signals_unavailable(monkeypatch):
    monkeypatch.setattr(concurrency, "_HAS_SETITIMER", False)
    with pytest.raises(RuntimeError):
        timeout(1, use_signals=True)

    @timeout(0.02)  # falls back to a worker thread
    def f():
        return threading.current_thread().name

    assert f().startswith("timeout-")


def test_timeout_thread_fallback_times_out_and_keeps_running():
    release = threading.Event()
    finished = threading.Event()

    @timeout(0.02, use_signals=False)
    def slow():
        release.wait(1)
        finished.set()

    with pytest.raises(TimeoutExpired):
        slow()
    assert not finished.is_set()
    release.set()
    assert finished.wait(1)  # the work kept running in the background


def test_timeout_in_worker_thread_uses_fallback():
    @timeout(0.02)
    def slow():
        time.sleep(0.04)

    future = run_in_thread(slow)()
    assert isinstance(future.exception(timeout=1), TimeoutExpired)


async def test_timeout_async():
    @timeout(0.02)
    async def slow():
        await asyncio.sleep(1)

    @timeout(1)
    async def fast(x):
        return x

    assert inspect.iscoroutinefunction(slow)
    assert await fast(3) == 3
    with pytest.raises(TimeoutExpired) as info:
        await slow()
    assert info.value.seconds == 0.02


def test_timeout_on_methods():
    class C:
        @timeout(1, use_signals=False)
        def m(self, x):
            return x * 2

        @classmethod
        @timeout(1)
        def cm(cls):
            return cls

        @staticmethod
        @timeout(1)
        def sm():
            return "s"

    assert C().m(2) == 4
    assert C.cm() is C
    assert C.sm() == "s"


# --------------------------------------------------------------------------------------
# rate_limit
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("calls", "period"), [(0, 1), (1, 0), (1, -1)])
def test_rate_limit_validates(calls, period):
    with pytest.raises(ValueError):
        rate_limit(calls, period)


def test_rate_limit_rejects_with_retry_after(fake_clock):
    @rate_limit(2, 10)
    def f(x):
        return x

    assert f(1) == 1
    fake_clock.advance(4)
    assert f(2) == 2
    with pytest.raises(RateLimitExceeded) as info:
        f(3)
    assert info.value.retry_after == pytest.approx(6)
    fake_clock.advance(6)
    assert f(4) == 4  # first slot freed: sliding window
    with pytest.raises(RateLimitExceeded) as info:
        f(5)
    assert info.value.retry_after == pytest.approx(4)


def test_rate_limit_block_sleeps(fake_clock):
    @rate_limit(1, 5, block=True)
    def f():
        return fake_clock.now

    start = fake_clock.now
    assert f() == start
    assert f() == start + 5
    assert fake_clock.sleeps == [5]


def test_rate_limit_per_key(fake_clock):
    @rate_limit(1, 10, key=lambda user, **_: user)
    def send(user, text=""):
        return user

    assert send("a") == "a"
    assert send("b") == "b"
    with pytest.raises(RateLimitExceeded):
        send("a", text="again")


def test_rate_limit_sweeps_stale_buckets(fake_clock, monkeypatch):
    monkeypatch.setattr(concurrency, "_SWEEP_EVERY", 3)
    window = concurrency._SlidingWindow(1, 1)
    window.acquire("a")
    window.acquire("b")
    fake_clock.advance(2)
    window.acquire("c")  # third call triggers a sweep
    assert set(window._buckets) == {"c"}


async def test_rate_limit_async(fake_clock):
    @rate_limit(1, 3)
    async def f():
        return "x"

    @rate_limit(1, 3, block=True)
    async def g():
        return fake_clock.now

    assert inspect.iscoroutinefunction(f)
    assert await f() == "x"
    with pytest.raises(RateLimitExceeded):
        await f()
    start = fake_clock.now
    assert await g() == start
    assert await g() == start + 3


def test_rate_limit_thread_safe(fake_clock):
    hits = []

    @rate_limit(50, 100)
    def f():
        hits.append(1)

    barrier = threading.Barrier(8)
    rejected = []

    def worker():
        barrier.wait()
        for _ in range(20):
            try:
                f()
            except RateLimitExceeded:
                rejected.append(1)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(hits) == 50
    assert len(rejected) == 160 - 50


def test_rate_limit_shared_across_instances(fake_clock):
    class C:
        @rate_limit(1, 10)
        def m(self):
            return self

    a, b = C(), C()
    assert a.m() is a
    with pytest.raises(RateLimitExceeded):
        b.m()


# --------------------------------------------------------------------------------------
# run_in_thread
# --------------------------------------------------------------------------------------


def test_run_in_thread_returns_future_with_result():
    @run_in_thread
    def work(a, b=0):
        return a + b, threading.current_thread().name

    future = work(1, b=2)
    assert isinstance(future, concurrent.futures.Future)
    value, name = future.result(timeout=1)
    assert value == 3
    assert name.startswith("run_in_thread-")
    assert name != threading.current_thread().name


def test_run_in_thread_exception():
    @run_in_thread()
    def boom():
        raise ValueError("bad")

    with pytest.raises(ValueError, match="bad"):
        boom().result(timeout=1)


def test_run_in_thread_options():
    seen = {}

    @run_in_thread(daemon=True, name="worker-x")
    def work():
        t = threading.current_thread()
        seen["daemon"], seen["name"] = t.daemon, t.name

    work().result(timeout=1)
    assert seen == {"daemon": True, "name": "worker-x"}


def test_run_in_thread_async_function():
    @run_in_thread
    async def work(x):
        await asyncio.sleep(0)
        return x * 2

    assert work(21).result(timeout=1) == 42


def test_run_in_thread_cancelled_before_start(monkeypatch):
    started = []

    class LazyThread:
        def __init__(self, target, name, daemon):
            self.target = target
            started.append(self)

        def start(self):
            pass

    monkeypatch.setattr(concurrency.threading, "Thread", LazyThread)
    calls = []

    @run_in_thread
    def work():
        calls.append(1)

    future = work()
    assert future.cancel()
    started[0].target()  # the thread finally runs: sees the cancellation
    assert calls == []


def test_run_in_thread_method():
    class C:
        @run_in_thread
        def m(self, x):
            return self, x

    c = C()
    assert c.m(1).result(timeout=1) == (c, 1)


# --------------------------------------------------------------------------------------
# throttle
# --------------------------------------------------------------------------------------


def test_throttle_validates():
    with pytest.raises(ValueError):
        throttle(0)


def test_throttle_skips_within_interval(fake_clock):
    calls = []

    @throttle(5)
    def f(x):
        calls.append(x)
        return x

    assert f(1) == 1
    fake_clock.advance(4.9)
    assert f(2) == 1  # skipped: last result
    fake_clock.advance(0.1)
    assert f(3) == 3
    assert calls == [1, 3]


def test_throttle_returns_none_before_first_result(fake_clock):
    entered = threading.Event()
    release = threading.Event()

    @throttle(5)
    def f():
        entered.set()
        release.wait(1)
        return "done"

    future = run_in_thread(f)()
    assert entered.wait(1)
    assert f() is None  # first call still running
    release.set()
    assert future.result(timeout=1) == "done"
    assert f() == "done"


def test_throttle_exception_uses_window(fake_clock):
    calls = []

    @throttle(5)
    def f():
        calls.append(1)
        raise RuntimeError

    with pytest.raises(RuntimeError):
        f()
    assert f() is None
    assert calls == [1]


async def test_throttle_async(fake_clock):
    calls = []

    @throttle(2)
    async def f(x):
        calls.append(x)
        return x

    assert inspect.iscoroutinefunction(f)
    assert await f(1) == 1
    assert await f(2) == 1
    fake_clock.advance(2)
    assert await f(3) == 3
    assert calls == [1, 3]


def test_throttle_thread_safe(fake_clock):
    calls = []
    barrier = threading.Barrier(8)

    @throttle(10)
    def f():
        calls.append(1)

    def worker():
        barrier.wait()
        for _ in range(50):
            f()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == [1]


def test_throttle_method(fake_clock):
    class C:
        @throttle(1)
        def m(self):
            return self

    c = C()
    assert c.m() is c


# --------------------------------------------------------------------------------------
# debounce
# --------------------------------------------------------------------------------------


def test_debounce_validates():
    with pytest.raises(ValueError):
        debounce(-1)


def test_debounce_runs_last_call_once():
    calls = []
    done = threading.Event()

    @debounce(0.02)
    def f(x):
        calls.append(x)
        done.set()

    assert f(1) is None
    f(2)
    f(3)
    assert calls == []
    assert done.wait(1)
    time.sleep(0.03)
    assert calls == [3]


def test_debounce_cancel_and_flush():
    calls = []

    @debounce(10)
    def f(x):
        calls.append(x)
        return x * 10

    f(1)
    f.cancel()
    assert f.flush() is None
    f(2)
    f(3)
    assert f.flush() == 30
    assert f.flush() is None
    assert calls == [3]
    f.cancel()  # nothing pending: no-op


def test_debounce_superseded_timer_callback_is_ignored(monkeypatch):
    timers = []

    class ManualTimer:
        def __init__(self, interval, function, args):
            self.function, self.args = function, args
            self.daemon = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            pass

        def fire(self):
            self.function(*self.args)

    monkeypatch.setattr(concurrency.threading, "Timer", ManualTimer)
    calls = []

    @debounce(1)
    def f(x):
        calls.append(x)

    f(1)
    f(2)
    timers[0].fire()  # superseded: ignored
    assert calls == []
    timers[1].fire()
    assert calls == [2]
    timers[1].fire()  # already ran
    assert calls == [2]


def test_debounce_method():
    class C:
        def __init__(self):
            self.seen = []

        @debounce(10)
        def m(self, x):
            self.seen.append(x)

    c = C()
    c.m(1)
    c.m(2)
    c.m.flush()
    assert c.seen == [2]


async def test_debounce_async_burst_shares_result():
    calls = []

    @debounce(0.02)
    async def f(x):
        calls.append(x)
        return x * 10

    assert inspect.iscoroutinefunction(f)
    results = await asyncio.gather(f(1), f(2), f(3))
    assert results == [30, 30, 30]
    assert calls == [3]
    assert await f(4) == 40  # a new burst
    assert calls == [3, 4]


async def test_debounce_async_exception_shared():
    @debounce(0.01)
    async def f():
        raise KeyError("x")

    results = await asyncio.gather(f(), f(), return_exceptions=True)
    assert all(isinstance(r, KeyError) for r in results)


async def test_debounce_async_flush_and_cancel():
    calls = []

    @debounce(10)
    async def f(x):
        calls.append(x)
        return x

    assert await f.flush() is None
    t1 = asyncio.create_task(f(1))
    t2 = asyncio.create_task(f(2))
    await asyncio.sleep(0)
    assert await f.flush() == 2
    assert await t1 == 2
    assert await t2 == 2

    t3 = asyncio.create_task(f(3))
    await asyncio.sleep(0)
    f.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t3
    assert calls == [2]
    f.cancel()  # nothing pending


async def test_debounce_async_cancel_outside_loop_context():
    @debounce(10)
    async def f():
        return 1

    task = asyncio.create_task(f())
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()
    # cancel() from a thread with no running loop clears every loop's burst.
    await loop.run_in_executor(None, f.cancel)
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_debounce_async_caller_cancel_does_not_cancel_burst():
    calls = []

    @debounce(0.01)
    async def f(x):
        calls.append(x)
        return x

    t1 = asyncio.create_task(f(1))
    await asyncio.sleep(0)
    t1.cancel()
    assert await f(2) == 2
    assert calls == [2]


async def test_debounce_async_function_cancelled_while_running():
    started = asyncio.Event()

    @debounce(0)
    async def f():
        started.set()
        await asyncio.sleep(10)

    task = asyncio.create_task(f())
    await started.wait()
    timer_tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for t in timer_tasks:
        if t is not task:
            t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------------------
# singleflight
# --------------------------------------------------------------------------------------


def _run_concurrently(n, call):
    """Start n threads that each call ``call()``; return their futures."""
    return [run_in_thread(call)() for _ in range(n)]


def test_singleflight_threads_share_one_execution():
    release = threading.Event()
    arrived = []
    all_arrived = threading.Event()
    calls = []
    n = 6

    def key(x):
        arrived.append(x)
        if len(arrived) == n:
            all_arrived.set()
        return x

    @singleflight(key=key)
    def load(x):
        calls.append(x)
        release.wait(1)
        return object()

    futures = _run_concurrently(n, lambda: load("k"))
    assert all_arrived.wait(1)
    time.sleep(0.02)  # let every caller register on the in-flight call
    release.set()
    results = [f.result(timeout=1) for f in futures]
    assert len(calls) == 1
    assert all(r is results[0] for r in results)
    # finished: next call runs fresh
    assert load("k") is not results[0]
    assert len(calls) == 2


def test_singleflight_threads_share_exception():
    release = threading.Event()
    entered = threading.Event()
    calls = []

    @singleflight
    def boom(x):
        calls.append(x)
        entered.set()
        release.wait(1)
        raise ValueError(x)

    leader = run_in_thread(boom)(1)
    assert entered.wait(1)
    follower = run_in_thread(boom)(1)
    time.sleep(0.02)
    release.set()
    with pytest.raises(ValueError):
        leader.result(timeout=1)
    with pytest.raises(ValueError):
        follower.result(timeout=1)
    assert calls == [1]
    release.clear()
    release.set()
    with pytest.raises(ValueError):
        boom(1)
    assert calls == [1, 1]


def test_singleflight_different_keys_run_separately():
    calls = []

    @singleflight
    def f(x, *, y=0):
        calls.append((x, y))
        return x + y

    assert f(1, y=2) == 3
    assert f(1) == 1
    assert calls == [(1, 2), (1, 0)]


def test_singleflight_method():
    class C:
        @singleflight
        def m(self, x):
            return self, x

    c = C()
    assert c.m(1) == (c, 1)


async def test_singleflight_async_shares_task():
    calls = []
    release = asyncio.Event()

    @singleflight
    async def load(x):
        calls.append(x)
        await release.wait()
        return object()

    assert inspect.iscoroutinefunction(load)
    tasks = [asyncio.create_task(load("k")) for _ in range(5)]
    other = asyncio.create_task(load("other"))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)
    await other
    assert all(r is results[0] for r in results)
    assert calls == ["k", "other"]
    assert await load("k") is not results[0]


async def test_singleflight_async_exception_and_caller_cancel():
    release = asyncio.Event()
    calls = []

    @singleflight
    async def boom():
        calls.append(1)
        await release.wait()
        raise KeyError("x")

    t1 = asyncio.create_task(boom())
    t2 = asyncio.create_task(boom())
    await asyncio.sleep(0)
    t1.cancel()  # cancelling one caller does not cancel the shared work
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(KeyError):
        await t2
    with pytest.raises(asyncio.CancelledError):
        await t1
    assert calls == [1]
