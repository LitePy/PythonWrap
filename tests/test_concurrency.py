from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import gc
import inspect
import signal
import sys
import threading
import time
import types

import pytest

from python_wrap import _core, concurrency
from python_wrap.concurrency import (
    RateLimiter,
    Timeout,
    debounce,
    rate_limit,
    run_in_thread,
    singleflight,
    throttle,
    timeout,
)
from python_wrap.errors import RateLimitExceeded, TimeoutExpired
from python_wrap.testing import disable_rate_limits, fast_mode

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


def test_timeout_thread_fallback_ignores_fake_clock_sleeps(fake_clock):
    @timeout(1, use_signals=False)
    def work():
        _core.sleep(10)  # e.g. a retry backoff inside the limit: fake time only
        return "ok"

    assert work() == "ok"  # finished quickly in real time, so it is not late
    assert fake_clock.sleeps == [10]


@pytest.mark.skipif(not hasattr(signal, "setitimer"), reason="needs SIGALRM")
def test_timeout_signal_nested_deadline_is_real_time_under_fake_clock(fake_clock):
    @timeout(0.02)
    def inner():
        time.sleep(1)  # real blocking: the inner deadline must fire

    @timeout(5)
    def outer():
        inner()

    start = time.monotonic()
    with pytest.raises(TimeoutExpired, match=r"within 0\.02s"):
        outer()
    assert time.monotonic() - start < 0.5


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


async def test_timeout_async_inner_timeout_error_propagates_unchanged():
    error = TimeoutError("db socket timed out")

    @timeout(5)
    async def f():
        raise error

    with pytest.raises(TimeoutError) as info:
        await f()
    assert info.value is error  # not converted to TimeoutExpired
    assert not isinstance(info.value, TimeoutExpired)
    assert info.traceback[-1].name == "f"  # original traceback kept


async def test_timeout_async_expiry_is_chained_not_hidden():
    @timeout(0.01)
    async def slow():
        await asyncio.sleep(1)

    with pytest.raises(TimeoutExpired) as info:
        await slow()
    if sys.version_info >= (3, 11):
        assert isinstance(info.value.__cause__, TimeoutError)


async def test_timeout_async_inner_exception_propagates():
    @timeout(5)
    async def f():
        await asyncio.sleep(0)
        raise KeyError("k")

    with pytest.raises(KeyError):
        await f()


async def test_timeout_async_caller_cancellation_is_not_a_timeout():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    @timeout(5)
    async def f():
        started.set()
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(f())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()  # the inner call was cancelled too


async def test_timeout_async_callable_object():
    class Slow:
        async def __call__(self):
            await asyncio.sleep(1)
            return "finished"

    wrapped = timeout(0.01)(Slow())
    assert inspect.iscoroutinefunction(wrapped)
    with pytest.raises(TimeoutExpired):
        await wrapped()


@needs_signals
def test_timeout_signal_swallowed_by_broad_except_in_user_code():
    # Documented limitation: TimeoutExpired is an Exception raised inside the function,
    # so a broad ``except Exception`` there catches it and the call returns normally.
    caught = []

    @timeout(0.01, use_signals=True)
    def work():
        try:
            time.sleep(1)
        except Exception as exc:
            caught.append(exc)
        return "returned normally"

    assert work() == "returned normally"
    assert len(caught) == 1 and isinstance(caught[0], TimeoutExpired)
    assert issubclass(TimeoutExpired, TimeoutError)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


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


def test_rate_limit_block_under_fast_mode_admits_without_spinning(monkeypatch):
    sleeps = []
    real_sleep = _core.sleep
    monkeypatch.setattr(_core, "sleep", lambda s: (sleeps.append(s), real_sleep(s)))
    limiter = RateLimiter(1, 60, block=True)
    events = []

    @limiter
    def f():
        return 1

    _core.listeners.append(lambda e: events.append(e.name))
    try:
        with fast_mode():
            start = time.monotonic()
            assert [f(), f(), f()] == [1, 1, 1]
            assert time.monotonic() - start < 1
            assert not limiter.try_acquire()  # the admitted calls were counted
    finally:
        _core.listeners.pop()
    assert len(sleeps) == 2  # one (no-op) sleep per blocked call, no busy loop
    assert events.count("rate_limit.waited") == 2


async def test_rate_limit_block_async_under_fast_mode_admits_without_spinning(monkeypatch):
    sleeps = []
    real_sleep = _core.async_sleep

    async def counting(s):
        sleeps.append(s)
        await real_sleep(s)

    monkeypatch.setattr(_core, "async_sleep", counting)
    limiter = RateLimiter(1, 60, block=True)

    @limiter
    async def g():
        return 1

    with fast_mode():
        start = time.monotonic()
        assert [await g(), await g(), await g()] == [1, 1, 1]
        assert time.monotonic() - start < 1
        assert not limiter.try_acquire()
    assert len(sleeps) == 2


def test_rate_limit_block_under_fast_mode_and_fake_clock_advances_it(fake_clock):
    @rate_limit(1, 5, block=True)
    def f():
        return fake_clock.now

    with fast_mode():
        start = fake_clock.now
        assert f() == start
        assert f() == start + 5  # the wait advances the fake clock
    assert fake_clock.sleeps == [5]


async def test_rate_limit_block_async_under_fast_mode_and_fake_clock(fake_clock):
    @rate_limit(1, 5, block=True)
    async def g():
        return fake_clock.now

    with fast_mode():
        start = fake_clock.now
        assert await g() == start
        assert await g() == start + 5
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
    window = concurrency._SlidingWindow(((1, 1.0),))
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


def test_throttle_in_window_after_raise_returns_last_successful_result(fake_clock):
    outcomes = iter(["a", ValueError("x")])

    @throttle(1)
    def f():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    assert f() == "a"
    fake_clock.advance(2)
    with pytest.raises(ValueError):
        f()
    assert f() == "a"  # the older successful result, not None


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


async def test_debounce_async_quiet_period_is_real_time_under_fast_mode():
    calls = []

    @debounce(0.05)
    async def f(x):
        calls.append(x)
        return x

    async def later(x, delay):
        await asyncio.sleep(delay)  # real time, inside the quiet period
        return await f(x)

    with fast_mode():
        assert await asyncio.gather(f(1), later(2, 0.01)) == [2, 2]
    assert calls == [2]  # one burst, as with the sync `threading.Timer`


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


def test_singleflight_default_key_keeps_kwargs_apart_from_lookalike_positionals():
    assert concurrency._default_key(1, b=2) != concurrency._default_key((1,), (("b", 2),))
    assert concurrency._default_key(1, b=2) == concurrency._default_key(1, b=2)
    assert concurrency._default_key(1, 2) == (1, 2)  # positional calls: the args tuple


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


async def test_singleflight_async_orphaned_failure_is_not_logged():
    reported = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    release = asyncio.Event()

    @singleflight
    async def load():
        await release.wait()
        raise ValueError("backend down")

    caller = asyncio.create_task(load())
    await asyncio.sleep(0)
    caller.cancel()  # every caller gone; the shared task keeps running
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    for _ in range(3):
        await asyncio.sleep(0)
    gc.collect()
    await asyncio.sleep(0)
    loop.set_exception_handler(None)
    assert reported == []


def test_singleflight_sync_reentrant_same_key_runs_directly():
    depth = []

    @singleflight
    def f(x):
        depth.append(x)
        if len(depth) < 3:
            return f(x)  # same key, on the leader's own thread
        return len(depth)

    future = run_in_thread(f)("k")  # guard: a regression must not hang the suite
    assert future.result(timeout=2) == 3
    assert f("again") == 4  # bookkeeping was cleaned up


async def test_singleflight_async_reentrant_same_key_runs_directly():
    depth = []

    @singleflight
    async def f(x):
        depth.append(x)
        if len(depth) < 3:
            return await f(x)  # same key, from inside the shared task
        return len(depth)

    assert await asyncio.wait_for(f("k"), 2) == 3


# --------------------------------------------------------------------------------------
# 0.2 features: timeout as a context manager, RateLimiter, multiple limits, overrides
# --------------------------------------------------------------------------------------


@pytest.fixture
def events():
    seen = []
    _core.listeners.append(seen.append)
    try:
        yield seen
    finally:
        _core.listeners.remove(seen.append)


def test_timeout_object_is_reusable_decorator_and_repr():
    limit = timeout(1)
    assert isinstance(limit, Timeout)
    assert limit(lambda: 1)() == 1
    assert limit(lambda: 2)() == 2
    assert "seconds=1" in repr(limit)


@needs_signals
def test_timeout_sync_block_interrupts_and_restores(events):
    before = signal.getsignal(signal.SIGALRM)
    started = time.perf_counter()
    with pytest.raises(TimeoutExpired) as info, timeout(0.02):
        time.sleep(1)
    assert time.perf_counter() - started < 0.5
    assert info.value.seconds == 0.02
    assert "<block>" in str(info.value)
    assert signal.getsignal(signal.SIGALRM) is before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert [(e.name, e.function, e.fields) for e in events] == [
        ("timeout.expired", "<block>", {"seconds": 0.02})
    ]


@needs_signals
def test_timeout_sync_block_passes_and_nests():
    limit = timeout(5)
    with limit as entered:
        assert entered is limit
        with pytest.raises(TimeoutExpired), timeout(0.02):
            time.sleep(1)
        remaining, _ = signal.getitimer(signal.ITIMER_REAL)
        assert 4 < remaining <= 5  # the outer timer was re-armed
        with limit:  # the same object can be entered again while active
            pass
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_timeout_sync_block_needs_signals_and_main_thread(monkeypatch):
    with pytest.raises(RuntimeError, match="main thread"), timeout(1, use_signals=False):
        pass

    def in_thread():
        with timeout(1):
            pass

    assert isinstance(run_in_thread(in_thread)().exception(timeout=1), RuntimeError)
    monkeypatch.setattr(concurrency, "_HAS_SETITIMER", False)
    with pytest.raises(RuntimeError), timeout(1):
        pass


async def test_timeout_async_block(events):
    started = time.perf_counter()
    with pytest.raises(TimeoutExpired) as info:
        async with timeout(0.02):
            await asyncio.sleep(1)
    assert time.perf_counter() - started < 0.5
    assert info.value.seconds == 0.02
    assert isinstance(info.value.__cause__, TimeoutError)
    assert [e.name for e in events] == ["timeout.expired"]

    limit = timeout(1)
    async with limit as entered:
        assert entered is limit
        await asyncio.sleep(0)
    assert limit._timers == {}


async def test_timeout_async_block_inner_errors_propagate_unchanged():
    with pytest.raises(TimeoutError) as info:
        async with timeout(1):
            raise TimeoutError("mine")
    assert type(info.value) is TimeoutError
    with pytest.raises(KeyError):
        async with timeout(1):
            raise KeyError


async def test_timeout_async_block_shared_by_concurrent_tasks():
    limit = timeout(0.05)

    async def quick(n):
        async with limit:
            await asyncio.sleep(0)
            return n

    async def slow():
        async with limit:
            await asyncio.sleep(1)

    results = await asyncio.gather(quick(1), slow(), quick(2), return_exceptions=True)
    assert results[0] == 1 and results[2] == 2
    assert isinstance(results[1], TimeoutExpired)
    assert limit._timers == {}


def test_timeout_worker_expiry_emits_event(events):
    @timeout(0.02, use_signals=False)
    def slow():
        time.sleep(0.1)

    with pytest.raises(TimeoutExpired):
        slow()
    assert [e.name for e in events] == ["timeout.expired"]
    assert events[0].function.endswith("slow")


# -- RateLimiter -----------------------------------------------------------------------


def test_rate_limiter_validates():
    with pytest.raises(TypeError):
        RateLimiter()
    with pytest.raises(TypeError):
        RateLimiter(5)
    with pytest.raises(ValueError):
        RateLimiter(limits=[(1, 1.0), (0, 5.0)])
    with pytest.raises(TypeError):
        rate_limit()
    assert "limits=[(2, 1.0)]" in repr(RateLimiter(2, 1.0))


def test_rate_limiter_shared_by_several_functions(fake_clock, events):
    limiter = RateLimiter(2, 10, name="api")

    @limiter
    def a():
        return "a"

    @limiter
    def b():
        return "b"

    assert a() == "a"
    assert b() == "b"
    with pytest.raises(RateLimitExceeded) as info:
        a()
    assert "api rate limit of 2 calls per 10s" in str(info.value)
    assert info.value.retry_after == pytest.approx(10)
    assert [(e.name, e.function) for e in events] == [("rate_limit.rejected", "api")]
    assert events[0].fields == {"retry_after": pytest.approx(10)}


def test_rate_limiter_try_acquire_and_acquire(fake_clock, events):
    limiter = RateLimiter(1, 5)
    assert limiter.try_acquire()
    assert not limiter.try_acquire()
    assert not limiter.acquire(block=False)
    assert not limiter.acquire(timeout=1)  # next slot is 5s away: give up at once
    assert fake_clock.sleeps == []
    start = fake_clock.now
    assert limiter.acquire()
    assert fake_clock.now == start + 5
    assert limiter.acquire(timeout=10)
    assert fake_clock.now == start + 10
    names = [e.name for e in events]
    assert names == ["rate_limit.rejected"] * 3 + ["rate_limit.waited"] * 2
    assert events[-1].fields == {"seconds": pytest.approx(5)}
    assert events[-1].function == "rate_limiter"


def test_rate_limiter_with_block(fake_clock):
    raising = RateLimiter(1, 5)
    with raising as entered:
        assert entered is raising
    with pytest.raises(RateLimitExceeded), raising:
        pass
    waiting = RateLimiter(1, 5, block=True)
    with waiting:
        pass
    with waiting:
        pass
    assert fake_clock.sleeps == [5]


async def test_rate_limiter_async(fake_clock, events):
    limiter = RateLimiter(1, 3, block=True, name="svc")

    @limiter
    async def f():
        return fake_clock.now

    assert inspect.iscoroutinefunction(f)
    start = fake_clock.now
    assert await f() == start
    async with limiter as entered:
        assert entered is limiter
        assert fake_clock.now == start + 3
    assert not await limiter.acquire_async(block=False)
    assert not await limiter.acquire_async(timeout=1)
    assert await limiter.acquire_async()
    assert fake_clock.now == start + 6
    strict = RateLimiter(1, 3)
    async with strict:
        pass
    with pytest.raises(RateLimitExceeded):
        async with strict:
            pass
    assert "rate_limit.waited" in [e.name for e in events]


def test_multiple_limits_all_must_allow(fake_clock):
    @rate_limit(limits=[(2, 1.0), (3, 10.0)])
    def f():
        return fake_clock.now

    f()
    f()
    with pytest.raises(RateLimitExceeded) as info:
        f()  # the 1-second limit is full
    assert info.value.retry_after == pytest.approx(1)
    assert "2 calls per 1s and 3 calls per 10s" in str(info.value)
    fake_clock.advance(1)
    f()
    with pytest.raises(RateLimitExceeded) as info:
        f()  # now the 10-second limit is full
    assert info.value.retry_after == pytest.approx(9)
    fake_clock.advance(9)
    f()


def test_multiple_limits_rejected_call_is_not_recorded(fake_clock):
    limiter = RateLimiter(5, 1.0, limits=[(1, 10.0)])
    assert limiter.try_acquire()
    for _ in range(10):
        assert not limiter.try_acquire()
    fake_clock.advance(10)
    assert limiter.try_acquire()  # rejected tries did not use up the 1-second limit


def test_multiple_limits_block_waits_for_longest(fake_clock):
    @rate_limit(1, 1.0, limits=[(2, 10.0)], block=True)
    def f():
        return fake_clock.now

    start = fake_clock.now
    assert [f(), f(), f()] == [start, start + 1, start + 10]


def test_rate_limit_key_with_multiple_limits(fake_clock):
    @rate_limit(limits=[(1, 5.0)], key=lambda user: user)
    def send(user):
        return user

    assert send("a") == "a"
    assert send("b") == "b"
    with pytest.raises(RateLimitExceeded):
        send("a")


def test_rate_limits_disabled_override(fake_clock):
    limiter = RateLimiter(1, 100)

    @rate_limit(1, 100)
    def f():
        return 1

    @rate_limit(1, 100, block=True)
    async def g():
        return 2

    @throttle(100)
    def t():
        return fake_clock.now

    assert f() == 1
    assert t() == fake_clock.now
    assert limiter.try_acquire()
    with disable_rate_limits():
        assert [f() for _ in range(5)] == [1] * 5
        assert all(limiter.try_acquire() for _ in range(5))
        with limiter:
            pass
        assert asyncio.run(g()) == 2
        assert asyncio.run(g()) == 2
        fake_clock.advance(1)
        assert t() == fake_clock.now  # throttle lets every call run
    assert fake_clock.sleeps == []
    with pytest.raises(RateLimitExceeded):
        f()
    assert not limiter.try_acquire()


def test_rate_limit_override_flipped_while_waiting(fake_clock, monkeypatch):
    limiter = RateLimiter(1, 100, block=True)
    limiter.try_acquire()

    def sleep(seconds):
        _core.overrides.disable_rate_limits = True

    monkeypatch.setattr(_core, "sleep", sleep)
    try:
        with limiter:
            pass
    finally:
        _core.overrides.disable_rate_limits = False


async def test_rate_limit_override_flipped_while_waiting_async(fake_clock, monkeypatch):
    limiter = RateLimiter(1, 100, block=True)
    limiter.try_acquire()

    async def async_sleep(seconds):
        _core.overrides.disable_rate_limits = True

    monkeypatch.setattr(_core, "async_sleep", async_sleep)
    try:
        assert await limiter.acquire_async()
    finally:
        _core.overrides.disable_rate_limits = False


def test_rate_limiter_thread_safe_shared_budget(fake_clock):
    limiter = RateLimiter(30, 100)
    hits = []

    @limiter
    def a():
        hits.append("a")

    @limiter
    def b():
        hits.append("b")

    barrier = threading.Barrier(6)

    def worker(fn):
        barrier.wait()
        for _ in range(20):
            with contextlib.suppress(RateLimitExceeded):
                fn()

    threads = [threading.Thread(target=worker, args=(a if i % 2 else b,)) for i in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(hits) == 30


def test_throttle_skipped_event(fake_clock, events):
    @throttle(10)
    def f():
        return 1

    f()
    f()
    f()
    assert [e.name for e in events] == ["throttle.skipped"] * 2
    assert events[0].function.endswith("f")


# --------------------------------------------------------------------------------------
# 0.2 performance: pooled timeout workers, cheaper singleflight, limiter access
# --------------------------------------------------------------------------------------


def _worker_thread(**opts):
    @timeout(1, use_signals=False, **opts)
    def which():
        return threading.current_thread()

    return which


def test_timeout_fallback_reuses_idle_worker_and_copies_context():
    import contextvars

    var = contextvars.ContextVar("var", default="unset")

    @timeout(1, use_signals=False)
    def probe():
        return threading.current_thread(), var.get()

    var.set("first")
    t1, v1 = probe()
    var.set("second")
    t2, v2 = probe()
    assert t1 is t2  # the idle worker was reused, not a new thread per call
    assert t1.name.startswith("timeout-") and t1.daemon
    assert (v1, v2) == ("first", "second")  # contextvars copied per call


def test_timeout_fallback_abandons_worker_of_timed_out_call():
    release = threading.Event()
    stuck = []

    @timeout(0.02, use_signals=False)
    def slow():
        stuck.append(threading.current_thread())
        release.wait(2)

    with pytest.raises(TimeoutExpired):
        slow()
    try:
        # The stuck worker is never handed out again while it is busy.
        assert _worker_thread()() is not stuck[0]
        assert all(t is not stuck[0] for t in (_worker_thread()() for _ in range(3)))
    finally:
        release.set()
    stuck[0].join(2)
    assert not stuck[0].is_alive()  # abandoned: it exits instead of rejoining the pool


def test_timeout_fallback_workers_exit_when_idle(monkeypatch):
    monkeypatch.setattr(concurrency, "_WORKER_IDLE_SECONDS", 0.02)
    first = _worker_thread()()
    second = _worker_thread()()  # the same worker is reused while it is idle
    assert first is second
    first.join(2)  # then it exits after being idle for _WORKER_IDLE_SECONDS
    assert not first.is_alive()
    assert _worker_thread()() is not first  # a new worker is started on demand


def test_timeout_fallback_keeps_a_bounded_idle_pool(monkeypatch):
    monkeypatch.setattr(concurrency, "_MAX_IDLE_WORKERS", 2)
    n = 6
    barrier = threading.Barrier(n)

    @timeout(2, use_signals=False)
    def wait_for_all():
        barrier.wait(2)
        return threading.current_thread()

    futures = [run_in_thread(wait_for_all)() for _ in range(n)]
    workers = {f.result(timeout=3) for f in futures}
    assert len(workers) == n
    with concurrency._workers.lock:
        assert len(concurrency._workers.idle) <= 2
    deadline = time.monotonic() + 2
    while sum(w.is_alive() for w in workers) > 2:  # the extra ones exit once done
        assert time.monotonic() < deadline
        time.sleep(0.001)


def test_timeout_fallback_propagates_exception_from_pooled_worker():
    @timeout(1, use_signals=False)
    def boom():
        raise LookupError("nope")

    with pytest.raises(LookupError, match="nope"):
        boom()
    with pytest.raises(LookupError):
        boom()  # the worker survived the exception and is reused


def test_timeout_worker_claimed_while_giving_up_still_runs_the_job():
    import queue as queue_mod

    pool = concurrency._WorkerPool()
    job = concurrency._Job(lambda: "ran", (), {})

    class Inbox:
        """The idle wait times out just as a caller claims the worker and sends a job."""

        def __init__(self):
            self.real = queue_mod.SimpleQueue()
            self.gets = 0

        def get(self, timeout):
            self.gets += 1
            if self.gets == 1:
                with pool.lock:
                    assert pool.idle.pop() is self
                self.real.put(job)
                raise queue_mod.Empty
            return self.real.get(timeout=timeout)

    inbox = Inbox()
    pool.idle.append(inbox)  # type: ignore[arg-type]
    job.abandoned = True  # so the worker exits once the job is done
    thread = threading.Thread(target=pool._work, args=(inbox,), daemon=True)
    thread.start()
    assert job.done.acquire(timeout=2)  # it kept waiting and ran the job
    assert job.result == "ran"
    thread.join(2)
    assert not thread.is_alive()


def test_timeout_worker_pool_reset_after_fork():
    pool = concurrency._WorkerPool()
    pool.idle.append(object())  # type: ignore[arg-type]
    pool.reset()
    assert pool.idle == []


@pytest.mark.skipif(sys.version_info < (3, 12), reason="eager tasks need Python 3.12+")
def test_singleflight_async_with_eager_task_factory():
    calls = []

    @singleflight
    async def load(k):
        calls.append(k)
        return len(calls)  # finishes without suspending

    async def main():
        asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)
        first = [await load(1) for _ in range(3)]
        release = asyncio.Event()

        @singleflight
        async def slow(k):
            calls.append(k)
            await release.wait()
            return k

        tasks = [asyncio.create_task(slow("s")) for _ in range(3)]
        release.set()
        return first, await asyncio.gather(*tasks)

    assert asyncio.run(main()) == ([1, 2, 3], ["s", "s", "s"])
    assert calls == [1, 1, 1, "s"]


async def test_singleflight_async_uncontended_needs_few_loop_iterations():
    @singleflight
    async def load(k):
        return k

    ticks = 0
    stop = False

    async def ticker():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0)

    t = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    before = ticks
    assert await load(1) == 1
    spent = ticks - before
    stop = True
    await t
    # 3.12+: the run starts eagerly and finishes before the caller waits at all.
    # Before: the run's step, then the caller's wake-up (it was 3 with a relay).
    assert spent <= (0 if sys.version_info >= (3, 12) else 2)


async def test_singleflight_async_only_caller_cancelled_run_carries_on():
    release = asyncio.Event()
    finished = []

    @singleflight
    async def load():
        await release.wait()
        finished.append(1)
        return 1

    caller = asyncio.create_task(load())
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    follower = asyncio.create_task(load())  # joins the still-running flight
    await asyncio.sleep(0)
    release.set()
    assert await follower == 1
    assert finished == [1]


async def test_singleflight_async_shared_run_cancelled_cancels_callers():
    started = asyncio.Event()
    runs = []

    @singleflight
    async def load():
        runs.append(1)
        started.set()
        if len(runs) == 1:
            await asyncio.sleep(10)
        return len(runs)

    callers = [asyncio.create_task(load()) for _ in range(2)]
    await started.wait()
    me = asyncio.current_task()
    (run_task,) = [t for t in asyncio.all_tasks() if t not in callers and t is not me]
    run_task.cancel()
    for c in callers:
        with pytest.raises(asyncio.CancelledError):
            await c
    assert await load() == 2  # the cancelled flight was cleaned up


def test_rate_limiter_reset_and_limiter_of(fake_clock):
    @rate_limit(1, 60.0)
    def ping():
        return "pong"

    assert ping() == "pong"
    with pytest.raises(RateLimitExceeded):
        ping()
    limiter = concurrency.limiter_of(ping)
    assert isinstance(limiter, RateLimiter)
    limiter.reset()
    assert ping() == "pong"

    shared = RateLimiter(1, 60.0)

    @shared
    async def aping():
        return "apong"

    assert concurrency.limiter_of(aping) is shared
    assert asyncio.run(aping()) == "apong"
    assert not shared.try_acquire()
    shared.reset()
    assert shared.try_acquire()

    class Client:
        @rate_limit(1, 60.0)
        def get(self):
            return 1

    assert concurrency.limiter_of(Client().get) is concurrency.limiter_of(Client.get)
    assert concurrency.limiter_of(lambda: None) is None


def test_circuit_breaker_wrapper_does_not_redetect_async(monkeypatch):
    from python_wrap.circuit import CircuitBreaker

    breaker = CircuitBreaker()

    @breaker
    def f():
        return 1

    def fail(_func):
        raise AssertionError("is_async called per call")

    monkeypatch.setattr(_core, "is_async", fail)
    assert f() == 1


async def test_singleflight_async_flight_outcome_races():
    loop = asyncio.get_running_loop()
    flight = concurrency._AsyncFlight()
    done = loop.create_future()
    done.cancel()
    pending = loop.create_future()
    flight.waiters += [done, pending]
    flight.deliver(5)  # skips the waiter that is already done
    assert pending.result() == 5 and flight.waiters == []

    # The outcome arrives in the same loop step as the caller's cancellation.
    waiter = loop.create_future()
    flight.waiters.append(waiter)
    caller = asyncio.create_task(concurrency._await_flight(flight, waiter))
    await asyncio.sleep(0)
    flight.deliver(exc=ValueError("late"))
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert waiter.exception() is not None  # retrieved, so never logged


def test_timeout_fallback_result_arriving_after_the_limit_is_a_timeout(monkeypatch, events):
    now = [100.0]  # the late-result check reads real time: fake it for this module only
    monkeypatch.setattr(concurrency, "time", types.SimpleNamespace(monotonic=lambda: now[0]))

    @timeout(1, use_signals=False)
    def late():
        now[0] += 2  # finishes, but only after its limit (an overslept wait)
        return "late"

    with pytest.raises(TimeoutExpired):
        late()
    assert [e.name for e in events] == ["timeout.expired"]
