"""Thread-safety stress tests for every stateful decorator.

Skipped unless ``PW_STRESS=1`` is set, so the normal test run stays fast::

    PW_STRESS=1 uv run pytest tests/stress -q
    PYTHON_GIL=0 PW_STRESS=1 uv run --python 3.14t --isolated --group dev \\
        pytest tests/stress -q -p no:cacheprovider

Each test starts 16-64 threads behind a `threading.Barrier` (so they all hit the
decorator at the same moment) and checks an invariant the decorator documents. Async
paths are exercised with several event loops, each running in its own thread. On a
GIL build the switch interval is lowered for the duration of each test to force more
interleavings.

Tests marked ``xfail(strict=True)`` reproduce a known race deterministically (by
forcing the bad interleaving) and document it until the code is fixed; fixing the
bug turns them into an XPASS failure, which is the cue to drop the marker.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
import tracemalloc
from collections import Counter
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest

from python_wrap import _core
from python_wrap.batching import batch_calls
from python_wrap.bulkhead import Bulkhead
from python_wrap.caching import memoize, once
from python_wrap.circuit import CircuitBreaker, CircuitState
from python_wrap.concurrency import debounce, rate_limit, singleflight, throttle
from python_wrap.errors import BulkheadFull, CircuitOpenError, RateLimitExceeded
from python_wrap.retry import RetryState, retry
from python_wrap.state import Transaction, current_transaction, transactional
from python_wrap.testing import FakeClock, Spy, fake_clock, overrides
from python_wrap.timing import MemoryRecord, measure_memory

pytestmark = pytest.mark.skipif(
    os.environ.get("PW_STRESS") != "1", reason="stress suite; set PW_STRESS=1 to run"
)

GIL_ENABLED: bool = getattr(sys, "_is_gil_enabled", lambda: True)()
JOIN_TIMEOUT = 10.0


# --------------------------------------------------------------------------- helpers


@pytest.fixture(autouse=True)
def _max_contention() -> Iterator[None]:
    """Switch threads as often as possible on GIL builds (no-op without a GIL)."""
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)


def hammer(n: int, target: Callable[[int], Any], *, timeout: float = JOIN_TIMEOUT) -> list[Any]:
    """Run ``target(i)`` in ``n`` threads released together; return results in order.

    Re-raises the first exception from a worker; fails if a worker hangs.
    """
    barrier = threading.Barrier(n)
    results: list[Any] = [None] * n
    errors: list[BaseException] = []

    def run(i: int) -> None:
        try:
            barrier.wait()
            results[i] = target(i)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    deadline = time.monotonic() + timeout
    for t in threads:
        t.join(max(deadline - time.monotonic(), 0.0))
    hung = sum(t.is_alive() for t in threads)
    assert hung == 0, f"{hung} worker thread(s) hung"
    if errors:
        raise errors[0]
    return results


def in_loops(n: int, main: Callable[[int], Coroutine[Any, Any, Any]]) -> list[Any]:
    """Run ``asyncio.run(main(i))`` in ``n`` threads at once: n event loops in parallel."""
    return hammer(n, lambda i: asyncio.run(main(i)))


class AtomicCounter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counts: Counter[Any] = Counter()

    def add(self, key: Any = None, n: int = 1) -> int:
        with self._lock:
            self.counts[key] += n
            return self.counts[key]

    def __getitem__(self, key: Any) -> int:
        with self._lock:
            return self.counts[key]

    @property
    def total(self) -> int:
        with self._lock:
            return sum(self.counts.values())


class Peak:
    """Tracks how many threads are inside a section at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.peak = 0

    def __enter__(self) -> None:
        with self._lock:
            self.current += 1
            self.peak = max(self.peak, self.current)

    def __exit__(self, *exc: object) -> None:
        with self._lock:
            self.current -= 1


def wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.001)


def test_report_interpreter(capsys: pytest.CaptureFixture[str]) -> None:
    with capsys.disabled():
        print(f"\n[stress] Python {sys.version.split()[0]} GIL enabled: {GIL_ENABLED}")
    assert bool(getattr(sys, "_is_gil_enabled", lambda: True)()) == GIL_ENABLED


# --------------------------------------------------------------------------- memoize


def test_memoize_sync_counters_add_up() -> None:
    computed = AtomicCounter()
    threads, calls, keys = 32, 400, 16

    @memoize
    def square(k: int) -> int:
        computed.add(k)
        return k * k

    def work(i: int) -> None:
        for j in range(calls):
            k = (i + j) % keys
            assert square(k) == k * k

    hammer(threads, work)
    info = square.cache_info()
    assert info.hits + info.misses == threads * calls
    assert info.misses == computed.total
    assert info.currsize == keys
    # Documented: concurrent misses on one key may both compute, at most once per thread.
    assert all(1 <= computed[k] <= threads for k in range(keys))


def test_memoize_sync_lru_bound_holds_under_contention() -> None:
    @memoize(maxsize=4, ttl=60)
    def ident(k: int) -> int:
        return k

    def work(i: int) -> None:
        for j in range(300):
            k = (i * 7 + j) % 32
            assert ident(k) == k
            assert ident.cache_info().currsize <= 4

    hammer(32, work)
    info = ident.cache_info()
    assert info.hits + info.misses == 32 * 300
    assert info.currsize <= 4


def test_memoize_sync_cache_clear_and_delete_while_calling() -> None:
    @memoize
    def triple(k: int) -> int:
        return 3 * k

    def work(i: int) -> None:
        for j in range(300):
            if i == 0 and j % 10 == 0:
                triple.cache_clear()
            elif i == 1:
                triple.cache_delete(j % 8)
            else:
                assert triple(j % 8) == 3 * (j % 8)

    hammer(24, work)
    assert triple.cache_info().currsize <= 8


def test_memoize_async_across_event_loops() -> None:
    computed = AtomicCounter()
    loops, tasks, keys = 8, 64, 8

    @memoize
    async def ident(k: int) -> int:
        computed.add(k)
        await asyncio.sleep(0.01)
        return k

    async def main(i: int) -> None:
        got = await asyncio.gather(*(ident(t % keys) for t in range(tasks)))
        assert got == [t % keys for t in range(tasks)]

    in_loops(loops, main)
    info = ident.cache_info()
    assert info.hits + info.misses == loops * tasks
    assert info.misses == computed.total
    # In-flight sharing is per event loop: each key is computed at most once per loop.
    assert all(1 <= computed[k] <= loops for k in range(keys))


needs_eager = pytest.mark.skipif(
    not hasattr(asyncio, "eager_task_factory"), reason="needs asyncio.eager_task_factory"
)


def _run_in_daemon(fn: Callable[[], Any], timeout: float = 2.0) -> Any:
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "deadlocked"
    if "error" in box:
        raise box["error"]
    return box.get("result")


@needs_eager
def test_memoize_async_with_eager_task_factory() -> None:
    @memoize
    async def double(k: int) -> int:
        return 2 * k  # finishes without suspending: runs entirely inside ensure_future

    async def main() -> int:
        asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)  # type: ignore[attr-defined]
        return await double(4)

    assert _run_in_daemon(lambda: asyncio.run(main())) == 8


# ------------------------------------------------------------------------------ once


def test_once_sync_runs_exactly_once() -> None:
    runs = AtomicCounter()

    @once
    def setup() -> object:
        runs.add()
        time.sleep(0.01)
        return object()

    results = hammer(64, lambda i: [setup() for _ in range(50)])
    first = results[0][0]
    assert all(r is first for rs in results for r in rs)
    assert runs.total == 1
    assert setup.called


def test_once_sync_reset_between_rounds() -> None:
    runs = AtomicCounter()

    @once
    def setup() -> int:
        return runs.add()

    for round_no in range(1, 31):
        results = hammer(16, lambda i: setup())
        assert results == [round_no] * 16
        setup.reset()
    assert runs.total == 30


def test_once_async_across_event_loops_runs_once() -> None:
    runs = AtomicCounter()

    @once
    async def setup() -> str:
        runs.add()
        await asyncio.sleep(0.05)
        return "ready"

    async def main(i: int) -> None:
        assert await asyncio.gather(*(setup() for _ in range(16))) == ["ready"] * 16

    in_loops(8, main)
    assert runs.total == 1


# -------------------------------------------------------------------------- throttle


def test_throttle_sync_runs_once_per_window() -> None:
    runs = AtomicCounter()

    @throttle(1000)
    def refresh() -> str:
        runs.add()
        return "fresh"

    results = hammer(64, lambda i: {refresh() for _ in range(50)})
    assert runs.total == 1
    assert set().union(*results) <= {None, "fresh"}


def test_throttle_async_across_event_loops() -> None:
    runs = AtomicCounter()

    @throttle(1000)
    async def refresh() -> str:
        runs.add()
        await asyncio.sleep(0)
        return "fresh"

    async def main(i: int) -> None:
        got = await asyncio.gather(*(refresh() for _ in range(32)))
        assert set(got) <= {None, "fresh"}

    in_loops(8, main)
    assert runs.total == 1


# ------------------------------------------------------------------------ rate_limit


def _attempts(fn: Callable[[], Any], n: int) -> int:
    admitted = 0
    for _ in range(n):
        try:
            fn()
        except RateLimitExceeded as exc:
            assert exc.retry_after > 0
        else:
            admitted += 1
    return admitted


def test_rate_limit_never_admits_more_than_calls() -> None:
    ran = AtomicCounter()

    @rate_limit(50, 1000)
    def send() -> None:
        ran.add()

    admitted = hammer(64, lambda i: _attempts(send, 20))
    assert sum(admitted) == ran.total == 50


def test_rate_limit_per_key_buckets() -> None:
    ran = AtomicCounter()

    @rate_limit(10, 1000, key=lambda user: user)
    def send(user: int) -> None:
        ran.add(user)

    hammer(64, lambda i: _attempts(lambda: send(i % 8), 20))
    assert all(ran[u] == 10 for u in range(8))


def test_rate_limit_sweep_with_many_keys() -> None:
    # 64 * 64 distinct keys > the internal sweep threshold: sweeping must not drop or
    # double-count live buckets while other threads use them.
    @rate_limit(1, 1000, key=lambda k: k)
    def send(k: tuple[int, int]) -> None:
        pass

    def work(i: int) -> int:
        return sum(_attempts(lambda j=j: send((i, j)), 2) for j in range(64))

    admitted = hammer(64, work)
    assert admitted == [64] * 64


def test_rate_limit_async_across_event_loops() -> None:
    ran = AtomicCounter()

    @rate_limit(25, 1000)
    async def send() -> None:
        ran.add()

    async def main(i: int) -> int:
        outcomes = await asyncio.gather(*(send() for _ in range(20)), return_exceptions=True)
        assert all(o is None or isinstance(o, RateLimitExceeded) for o in outcomes)
        return sum(o is None for o in outcomes)

    assert sum(in_loops(8, main)) == ran.total == 25


@pytest.mark.xfail(strict=True, reason="race: rate_limit reads the clock before taking its lock")
def test_rate_limit_stale_clock_reading_admits_extra_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """A call delayed on the limiter's lock is recorded with the time it *arrived*.

    Force it: one thread holds the limiter's lock (its key's ``__hash__`` blocks, and the
    key is hashed under the lock) while another reads the clock and queues for the lock.
    A full period later the queued call is admitted but logged a period in the past, so
    a second call at the same instant is admitted too: 2 calls inside one window with
    ``calls=1``.
    """
    now = [0.0]
    waiter_read_clock = threading.Event()
    waiter_ident: list[int] = []

    def clock() -> float:
        value = now[0]
        if waiter_ident and threading.get_ident() == waiter_ident[0]:
            waiter_read_clock.set()
        return value

    monkeypatch.setattr(_core, "monotonic", clock)
    release_lock = threading.Event()

    class BlockingKey:
        def __hash__(self) -> int:
            release_lock.wait(5)
            return 1

    admitted: list[tuple[str, float]] = []

    @rate_limit(1, 1.0, key=lambda k: k)
    def send(k: object) -> None:
        admitted.append((repr(k), now[0]))

    holder = threading.Thread(target=send, args=(BlockingKey(),))
    holder.start()
    time.sleep(0.05)  # the holder is now inside the limiter's lock

    def waiter() -> None:
        waiter_ident.append(threading.get_ident())
        send("a")

    w = threading.Thread(target=waiter)
    w.start()
    assert waiter_read_clock.wait(5)
    time.sleep(0.05)  # the waiter has read t=0.0 and is blocked on the lock
    now[0] = 1.0
    release_lock.set()
    holder.join(5)
    w.join(5)
    assert ("'a'", 1.0) in admitted  # admitted at t=1.0
    with pytest.raises(RateLimitExceeded):
        send("a")  # still t=1.0: the window already holds one call for "a"


# -------------------------------------------------------------------------- debounce


def test_debounce_sync_burst_runs_once() -> None:
    runs: list[tuple[int, int]] = []
    lock = threading.Lock()

    @debounce(0.05)
    def save(i: int, j: int) -> None:
        with lock:
            runs.append((i, j))

    def work(i: int) -> None:
        for j in range(30):
            save(i, j)

    hammer(32, work)
    time.sleep(0.25)
    assert len(runs) == 1
    assert runs[0][1] == 29  # some thread's last call
    assert save.flush() is None


def test_debounce_sync_flush_and_cancel_while_calling() -> None:
    runs = AtomicCounter()

    @debounce(0.02)
    def save(i: int) -> None:
        runs.add()

    def work(i: int) -> None:
        for _ in range(50):
            if i == 0:
                save.flush()
            elif i == 1:
                save.cancel()
            else:
                save(i)

    hammer(24, work)
    save.flush()
    time.sleep(0.1)
    before = runs.total
    time.sleep(0.1)
    assert runs.total == before  # nothing left pending, nothing fires twice
    assert before <= 22 * 50


def test_debounce_async_one_run_per_loop_burst() -> None:
    runs = AtomicCounter()

    @debounce(0.02)
    async def save(loop_no: int, n: int) -> tuple[int, int]:
        runs.add(loop_no)
        return loop_no, n

    async def main(i: int) -> None:
        got = await asyncio.gather(*(save(i, n) for n in range(40)))
        assert got == [(i, 39)] * 40  # every caller of the burst shares the final outcome

    in_loops(8, main)
    assert all(runs[i] == 1 for i in range(8))


@pytest.mark.xfail(strict=True, reason="race: debounce(async).cancel() from a thread is unsafe")
def test_debounce_async_cancel_from_another_thread() -> None:
    """``cancel()`` called outside the loop touches that loop's futures cross-thread.

    `asyncio.Future.cancel` is not thread-safe: the callbacks are queued with
    ``call_soon`` without waking the loop, so the awaiting caller stays blocked until
    something else wakes the loop (here: the 30 s debounce timer).
    """
    started = threading.Event()
    outcome: dict[str, BaseException | None] = {}

    @debounce(30)
    async def save() -> None:
        pass

    async def main() -> None:
        loop = asyncio.get_running_loop()
        loop.call_soon(started.set)
        try:
            await save()
            outcome["error"] = None
        except BaseException as exc:
            outcome["error"] = exc

    t = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    t.start()
    assert started.wait(5)
    time.sleep(0.05)
    save.cancel()
    t.join(1.0)
    assert not t.is_alive(), "awaiting caller was not woken by cancel()"
    assert isinstance(outcome["error"], asyncio.CancelledError)


# ---------------------------------------------------------------------- singleflight


def test_singleflight_sync_one_run_per_key_per_flight() -> None:
    runs = AtomicCounter()

    @singleflight
    def load(k: int) -> object:
        runs.add(k)
        time.sleep(0.2)
        return object()

    results = hammer(64, lambda i: (i % 4, load(i % 4)))
    for k in range(4):
        assert runs[k] == 1
        assert len({id(r) for kk, r in results if kk == k}) == 1


def test_singleflight_sync_shares_the_exception() -> None:
    runs = AtomicCounter()

    @singleflight
    def load() -> None:
        runs.add()
        time.sleep(0.2)
        raise LookupError("boom")

    def work(i: int) -> BaseException | None:
        try:
            load()
        except LookupError as exc:
            return exc
        return None

    errors = hammer(48, work)
    assert runs.total == 1
    assert len({id(e) for e in errors}) == 1 and errors[0] is not None


def test_singleflight_sync_sequential_flights() -> None:
    runs = AtomicCounter()

    @singleflight
    def load(k: int) -> int:
        runs.add(k)
        return k

    hammer(32, lambda i: [load(j % 4) for j in range(200)])
    # Not a cache: many flights; every call got the right value and nothing hung.
    assert 4 <= runs.total <= 32 * 200


def test_singleflight_async_across_event_loops() -> None:
    runs = AtomicCounter()

    @singleflight
    async def load(k: int) -> int:
        runs.add(k)
        await asyncio.sleep(0.05)
        return k * 10

    async def main(i: int) -> None:
        assert await asyncio.gather(*(load(t % 4) for t in range(64))) == [
            (t % 4) * 10 for t in range(64)
        ]

    in_loops(8, main)
    # De-duplication is per event loop: at most one run per key per loop.
    assert all(1 <= runs[k] <= 8 for k in range(4))


@needs_eager
@pytest.mark.xfail(strict=True, reason="race: singleflight(async) KeyError under eager tasks")
def test_singleflight_async_with_eager_task_factory() -> None:
    runs = AtomicCounter()

    @singleflight
    async def load(k: int) -> int:
        return runs.add()  # finishes without suspending

    async def main() -> list[int]:
        asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)  # type: ignore[attr-defined]
        return [await load(1) for _ in range(3)]

    assert asyncio.run(main()) == [1, 2, 3]


# -------------------------------------------------------------------- CircuitBreaker


@pytest.mark.parametrize("max_trials", [1, 3])
def test_circuit_half_open_trial_slots_never_exceeded(
    fake_clock: FakeClock, max_trials: int
) -> None:
    breaker = CircuitBreaker(
        failure_threshold=1, recovery_timeout=30, half_open_max_calls=max_trials
    )
    gate = threading.Event()
    entered = AtomicCounter()
    rejected = AtomicCounter()
    threads = 64

    @breaker
    def call(fail: bool) -> str:
        if fail:
            raise ValueError("down")
        entered.add()
        assert gate.wait(5)
        return "ok"

    with pytest.raises(ValueError):
        call(True)
    assert breaker.state is CircuitState.OPEN
    fake_clock.advance(31)

    def work(i: int) -> str:
        try:
            return call(False)
        except CircuitOpenError:
            rejected.add()
            return "rejected"

    results: list[str] = []
    runner = threading.Thread(target=lambda: results.extend(hammer(threads, work)))
    runner.start()
    wait_for(lambda: entered.total + rejected.total == threads)
    assert entered.total == max_trials  # never more trial calls than slots
    gate.set()
    runner.join(JOIN_TIMEOUT)
    assert results.count("ok") == max_trials
    assert breaker.state is CircuitState.CLOSED


def test_circuit_concurrent_failures_open_once() -> None:
    breaker = CircuitBreaker(failure_threshold=20, recovery_timeout=1000)
    reached = AtomicCounter()
    transitions: list[_core.Event] = []

    def listener(event: _core.Event) -> None:
        if event.name == "circuit.state_changed":
            transitions.append(event)

    @breaker
    def call() -> None:
        reached.add()
        raise ValueError("down")

    def work(i: int) -> Counter[str]:
        seen: Counter[str] = Counter()
        for _ in range(20):
            try:
                call()
            except ValueError:
                seen["failed"] += 1
            except CircuitOpenError:
                seen["rejected"] += 1
        return seen

    _core.listeners.append(listener)
    try:
        outcomes = hammer(64, work)
    finally:
        _core.listeners.remove(listener)
    total: Counter[str] = sum(outcomes, Counter())
    assert breaker.state is CircuitState.OPEN
    assert total["failed"] == reached.total >= 20
    assert total["failed"] + total["rejected"] == 64 * 20
    opened = [e for e in transitions if e.fields.get("new") in ("open", CircuitState.OPEN)]
    assert len(opened) <= 1  # events are optional here, but must not report duplicates


def test_circuit_async_trial_slots_across_event_loops() -> None:
    breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05, half_open_max_calls=2)
    peak = Peak()

    @breaker
    async def call(fail: bool) -> str:
        if fail:
            raise ValueError("down")
        with peak:
            await asyncio.sleep(0.2)
        return "ok"

    with pytest.raises(ValueError):
        asyncio.run(call(True))
    time.sleep(0.06)

    async def main(i: int) -> list[Any]:
        return await asyncio.gather(*(call(False) for _ in range(8)), return_exceptions=True)

    outcomes = [o for loop_out in in_loops(8, main) for o in loop_out]
    assert peak.peak <= 2
    assert sum(o == "ok" for o in outcomes) <= 2
    assert all(o == "ok" or isinstance(o, CircuitOpenError) for o in outcomes)


# -------------------------------------------------------------------------- Bulkhead


def test_bulkhead_sync_never_runs_more_than_max() -> None:
    bulkhead = Bulkhead(4)
    peak = Peak()

    @bulkhead
    def work_unit() -> None:
        with peak:
            pass

    hammer(64, lambda i: [work_unit() for _ in range(200)])
    assert peak.peak <= 4
    assert bulkhead.active == 0 and bulkhead.waiting == 0


def test_bulkhead_sync_queue_bound() -> None:
    bulkhead = Bulkhead(2, max_waiting=8)
    peak = Peak()
    worst_waiting = [0]

    @bulkhead
    def work_unit() -> None:
        with peak:
            worst_waiting[0] = max(worst_waiting[0], bulkhead.waiting)
            time.sleep(0.0005)

    def work(i: int) -> int:
        full = 0
        for _ in range(30):
            try:
                work_unit()
            except BulkheadFull:
                full += 1
        return full

    rejected = sum(hammer(48, work))
    assert peak.peak <= 2
    assert worst_waiting[0] <= 8
    assert rejected > 0
    assert bulkhead.active == 0 and bulkhead.waiting == 0


@pytest.mark.xfail(strict=True, reason="race: Bulkhead.release frees the slot before active -= 1")
def test_bulkhead_active_never_exceeds_max() -> None:
    """``release()`` returns the semaphore permit before decrementing ``active``.

    Force the window: pause the releasing thread right after the permit is returned; a
    second caller takes the slot and then sees ``active == 2`` with ``max_concurrent=1``.
    """
    bulkhead = Bulkhead(1)
    inner = bulkhead._thread_semaphore
    paused = threading.Event()
    resume = threading.Event()
    pausing: list[int] = []

    class PausingSemaphore:
        def acquire(self, *args: Any, **kwargs: Any) -> bool:
            return inner.acquire(*args, **kwargs)

        def release(self) -> None:
            inner.release()
            if threading.get_ident() in pausing:
                paused.set()
                resume.wait(5)

    bulkhead._thread_semaphore = PausingSemaphore()  # type: ignore[assignment]

    def first() -> None:
        pausing.append(threading.get_ident())
        with bulkhead:
            pass

    t = threading.Thread(target=first)
    t.start()
    try:
        if paused.wait(1):  # only reached while release() frees the permit first
            with bulkhead:
                assert bulkhead.active <= bulkhead.max_concurrent
    finally:
        resume.set()
        t.join(5)
    assert bulkhead.active == 0


def test_bulkhead_async_per_loop_limit() -> None:
    bulkhead = Bulkhead(3)
    peaks: dict[int, Peak] = {i: Peak() for i in range(8)}

    async def unit(i: int) -> None:
        async with bulkhead:
            with peaks[i]:
                await asyncio.sleep(0.002)

    async def main(i: int) -> None:
        await asyncio.gather(*(unit(i) for _ in range(40)))

    in_loops(8, main)
    assert all(p.peak <= 3 for p in peaks.values())
    assert bulkhead.active == 0 and bulkhead.waiting == 0


# ----------------------------------------------------------------------- batch_calls


def test_batch_calls_sync_every_caller_gets_its_own_result() -> None:
    batches: list[list[tuple[int, int]]] = []
    lock = threading.Lock()

    @batch_calls(max_size=16, max_wait=0.005)
    def load(items: list[tuple[int, int]]) -> list[str]:
        with lock:
            batches.append(list(items))
        return [f"{i}:{j}" for i, j in items]

    def work(i: int) -> None:
        for j in range(40):
            assert load((i, j)) == f"{i}:{j}"

    hammer(64, work)
    sent = [item for batch in batches for item in batch]
    assert len(sent) == len(set(sent)) == 64 * 40  # each item sent exactly once
    assert all(1 <= len(b) <= 16 for b in batches)
    assert load.pending == 0


def test_batch_calls_sync_dedupes_shared_keys() -> None:
    sent = AtomicCounter()

    @batch_calls(max_size=8, max_wait=0.01)
    def load(items: list[int]) -> dict[int, int]:
        for item in items:
            sent.add(item)
        return {item: item * 2 for item in items}

    hammer(64, lambda i: [load(j % 8) == (j % 8) * 2 or pytest.fail("wrong") for j in range(20)])
    assert load.pending == 0
    assert sent.total <= 64 * 20


def test_batch_calls_sync_flush_while_calling() -> None:
    @batch_calls(max_size=1000, max_wait=0.05)
    def load(items: list[int]) -> list[int]:
        return [-i for i in items]

    def work(i: int) -> None:
        for j in range(30):
            if i == 0:
                load.flush()
            else:
                assert load(i * 1000 + j) == -(i * 1000 + j)

    hammer(32, work)
    assert load.pending == 0


def test_batch_calls_async_across_event_loops() -> None:
    batches: list[list[tuple[int, int]]] = []
    lock = threading.Lock()

    @batch_calls(max_size=32, max_wait=0.005)
    async def load(items: list[tuple[int, int]]) -> list[int]:
        with lock:
            batches.append(list(items))
        await asyncio.sleep(0)
        return [i * 1000 + j for i, j in items]

    async def main(i: int) -> None:
        got = await asyncio.gather(*(load((i, j)) for j in range(200)))
        assert got == [i * 1000 + j for j in range(200)]

    in_loops(8, main)
    sent = [item for batch in batches for item in batch]
    assert len(sent) == len(set(sent)) == 8 * 200
    assert all(len({i for i, _ in batch}) == 1 for batch in batches)  # never spans loops
    assert load.pending == 0


# ----------------------------------------------------------------------- Transaction


def test_transactional_isolated_per_thread() -> None:
    committed = AtomicCounter()

    @transactional
    def unit(i: int, *, tx: Transaction) -> None:
        assert current_transaction() is tx
        tx.on_commit(committed.add, i)
        if i % 3 == 0:
            tx.on_rollback(committed.add, ("rolled", i))
            raise KeyError(i)

    def work(i: int) -> None:
        for _ in range(50):
            try:
                unit(i)
            except KeyError:
                assert i % 3 == 0
            assert current_transaction() is None

    hammer(48, work)
    for i in range(48):
        if i % 3 == 0:
            assert committed[i] == 0 and committed[("rolled", i)] == 50
        else:
            assert committed[i] == 50


def test_transaction_completes_only_once() -> None:
    """Two threads committing one transaction must run the callbacks once.

    ``commit()`` checks ``_state`` and then sets it without a lock. Force the window by
    pausing the first committer right after its check.
    """
    tx = Transaction()
    runs = AtomicCounter()
    tx.on_commit(runs.add)
    first_checked = threading.Event()
    resume = threading.Event()
    first: list[int] = []
    original_check = tx._check_open

    def check_then_pause() -> None:
        original_check()
        if threading.get_ident() in first:
            first_checked.set()
            resume.wait(1)

    tx._check_open = check_then_pause  # type: ignore[method-assign]
    errors: list[BaseException] = []

    def commit(is_first: bool) -> None:
        if is_first:
            first.append(threading.get_ident())
        try:
            tx.commit()
        except RuntimeError as exc:
            errors.append(exc)

    a = threading.Thread(target=commit, args=(True,))
    a.start()
    assert first_checked.wait(5)
    b = threading.Thread(target=commit, args=(False,))
    b.start()
    b.join(0.2)
    resume.set()
    a.join(5)
    b.join(5)
    assert runs.total == 1
    assert len(errors) == 1


def test_transaction_concurrent_commit_hammer() -> None:
    # Probabilistic companion of the test above; only checks that nothing crashes and
    # the state ends committed. Double runs are reported, not asserted.
    doubles = 0
    for _ in range(200):
        tx = Transaction()
        runs = AtomicCounter()
        tx.on_commit(runs.add)

        def work(i: int, tx: Transaction = tx) -> None:
            with contextlib.suppress(RuntimeError):
                tx.commit()

        hammer(16, work)
        assert tx.state == "committed"
        assert runs.total >= 1
        doubles += runs.total > 1
    if doubles:
        print(f"\n[stress] Transaction.commit ran callbacks more than once in {doubles}/200")


# ---------------------------------------------------------------- retry / RetryState


def test_retry_state_is_per_call() -> None:
    local = threading.local()
    states: dict[int, list[int]] = {}
    lock = threading.Lock()

    def record(state: RetryState) -> None:
        with lock:
            states.setdefault(threading.get_ident(), []).append(state.attempt)

    @retry(max_attempts=3, delay=0, on_retry=record)
    def flaky() -> str:
        local.n = getattr(local, "n", 0) + 1
        if local.n % 3:
            raise ConnectionError("try again")
        return "ok"

    results = hammer(48, lambda i: [flaky() for _ in range(20)])
    assert all(r == ["ok"] * 20 for r in results)
    assert len(states) == 48
    assert all(seq == [1, 2] * 20 for seq in states.values())


def test_retry_async_across_event_loops() -> None:
    attempts = AtomicCounter()

    @retry(max_attempts=4, delay=0)
    async def flaky(key: tuple[int, int]) -> tuple[int, int]:
        if attempts.add(key) < 3:
            raise ConnectionError("try again")
        return key

    async def main(i: int) -> None:
        keys = [(i, j) for j in range(32)]
        assert await asyncio.gather(*(flaky(k) for k in keys)) == keys

    in_loops(8, main)
    assert all(attempts[(i, j)] == 3 for i in range(8) for j in range(32))


# --------------------------------------------------------- measure_memory refcounting


def test_measure_memory_tracing_refcount() -> None:
    if tracemalloc.is_tracing():
        pytest.skip("tracemalloc already running")
    records: list[MemoryRecord] = []
    lock = threading.Lock()

    def keep(record: MemoryRecord) -> None:
        with lock:
            records.append(record)

    @measure_memory(callback=keep)
    def build(n: int) -> list[int]:
        assert tracemalloc.is_tracing()  # nobody stops tracing under a running call
        return [0] * n

    hammer(32, lambda i: [build(1000) for _ in range(20)])
    assert len(records) == 32 * 20
    assert not tracemalloc.is_tracing()


def test_measure_memory_leaves_external_tracing_running() -> None:
    if tracemalloc.is_tracing():
        pytest.skip("tracemalloc already running")

    @measure_memory
    def build() -> list[int]:
        return [0] * 100

    tracemalloc.start()
    try:
        hammer(32, lambda i: [build() for _ in range(20)])
        assert tracemalloc.is_tracing()  # started by us, so the decorator must not stop it
    finally:
        tracemalloc.stop()


# --------------------------------------------------------------- testing utilities


def test_fake_clock_sleep_is_thread_safe() -> None:
    with fake_clock() as clock:
        hammer(64, lambda i: [_core.sleep(0.5) for _ in range(100)])
        assert len(clock.sleeps) == 64 * 100
        assert clock.now - clock.start == pytest.approx(64 * 100 * 0.5)


def test_spy_counts_every_call() -> None:
    spy = Spy(lambda x: x)
    hammer(64, lambda i: [spy(i) for _ in range(100)])
    assert spy.call_count == 64 * 100


def test_override_scopes_nested_in_one_thread_while_others_read() -> None:
    stop = threading.Event()
    seen: set[bool] = set()

    def reader() -> None:
        while not stop.is_set():
            seen.add(_core.overrides.disable_caching)

    r = threading.Thread(target=reader)
    r.start()
    try:
        for _ in range(500):
            with overrides(disable_caching=True), overrides(no_sleep=True):
                assert _core.overrides.disable_caching and _core.overrides.no_sleep
    finally:
        stop.set()
        r.join(5)
    assert not _core.overrides.disable_caching and not _core.overrides.no_sleep
    assert seen <= {True, False}


def test_override_scopes_overlapping_across_threads_restore() -> None:
    """Scopes entered A, B and exited A, B (from two threads) leave the field set.

    Documented in `python_wrap.testing` ("the last one to exit wins"), but it means a
    ``@fast_mode()``-decorated helper called from worker threads leaks overrides into
    later tests.
    """
    a_in, b_in, a_out = threading.Event(), threading.Event(), threading.Event()

    def a() -> None:
        with overrides(disable_rate_limits=True):
            a_in.set()
            assert b_in.wait(5)
        a_out.set()

    def b() -> None:
        assert a_in.wait(5)
        with overrides(disable_rate_limits=True):
            b_in.set()
            assert a_out.wait(5)

    ta, tb = threading.Thread(target=a), threading.Thread(target=b)
    ta.start()
    tb.start()
    ta.join(5)
    tb.join(5)
    leaked = _core.overrides.disable_rate_limits
    _core.set_overrides(disable_rate_limits=False)
    assert not leaked
