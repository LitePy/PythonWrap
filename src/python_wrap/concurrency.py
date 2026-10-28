"""Concurrency and flow-control decorators.

* `timeout` — bound how long a call (or a `with` / `async with` block) may run.
* `rate_limit` — at most *N* calls per period, rejecting or waiting.
* `RateLimiter` — a rate limit shared by several functions and blocks.
* `run_in_thread` — start a call in a new thread and get a ``Future`` back.
* `throttle` — run at most once per interval; calls in between are skipped.
* `debounce` — run once activity has stopped for a while.
* `singleflight` — concurrent identical calls share one execution.

Every decorator works on plain functions, ``async def`` functions and methods. State
kept by a decorator (rate-limit buckets, throttle windows, pending debounced calls) belongs
to the decorated function, so on a method it is shared by every instance unless a ``key``
argument says otherwise.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import functools
import os
import queue
import signal
import sys
import threading
import time
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Hashable, Iterable
from types import FrameType, TracebackType
from typing import (
    Any,
    Concatenate,
    Final,
    Generic,
    ParamSpec,
    Protocol,
    TypeVar,
    cast,
    overload,
)

from python_wrap import _core
from python_wrap import deadline as _deadline
from python_wrap._core import F, P, R
from python_wrap.errors import RateLimitExceeded, TimeoutExpired

__all__ = [
    "AsyncDebounced",
    "Debounced",
    "Limit",
    "RateLimiter",
    "Timeout",
    "debounce",
    "limiter_of",
    "rate_limit",
    "run_in_thread",
    "singleflight",
    "throttle",
    "timeout",
]

T = TypeVar("T")
R_co = TypeVar("R_co", covariant=True)
P2 = ParamSpec("P2")

# ---------------------------------------------------------------------------------------
# timeout
# ---------------------------------------------------------------------------------------

_HAS_SETITIMER = hasattr(signal, "setitimer") and hasattr(signal, "SIGALRM")
_BLOCK = "<block>"  # the "function" name used in messages and events for `with timeout()`


def _signals_usable() -> bool:
    return _HAS_SETITIMER and threading.current_thread() is threading.main_thread()


def _timeout_message(label: str, seconds: float) -> str:
    return f"{label} did not finish within {seconds:g}s"


def _expired(label: str, seconds: float) -> TimeoutExpired:
    """Build the error for an expiry and emit ``timeout.expired``."""
    _core.emit("timeout.expired", label, seconds=seconds)
    return TimeoutExpired(_timeout_message(label, seconds), seconds=seconds)


class _Alarm:
    """One SIGALRM/``setitimer`` deadline, cooperating with enclosing ones.

    Enter it in the main thread only. The previous handler and any armed timer are
    restored on exit; an enclosing timer whose deadline comes first still fires.
    Entering costs two system calls (install the handler, arm the timer — arming also
    reports the enclosing timer, so no separate ``getitimer``) and so does exiting.
    """

    def __init__(self, label: str, seconds: float) -> None:
        self.label = label
        self.seconds = seconds
        self.error: TimeoutExpired | None = None  # built only when the alarm fires
        self._outer_fired = False
        # The enclosing timer's delay; None until known (our timer is not armed yet).
        self._previous_delay: float | None = None
        self._previous_interval = 0.0

    def __enter__(self) -> None:
        # The itimer counts real time, so the deadline arithmetic does too (a fake clock
        # installed by tests must not move it).
        self._start = time.monotonic()
        deadline = self._start + self.seconds
        self._previous_handler = previous_handler = signal.getsignal(signal.SIGALRM)

        def handler(signum: int, frame: FrameType | None) -> None:
            previous_delay = self._previous_delay
            if previous_delay is not None and (
                not previous_delay or time.monotonic() >= deadline - 1e-3
            ):
                self.error = TimeoutExpired(
                    _timeout_message(self.label, self.seconds), seconds=self.seconds
                )
                raise self.error
            # The alarm belongs to an enclosing timeout whose deadline comes first (or
            # fired before ours was even armed).
            self._outer_fired = True
            signal.signal(signal.SIGALRM, previous_handler)
            if callable(previous_handler):
                previous_handler(signum, frame)

        signal.signal(signal.SIGALRM, handler)
        previous_delay, self._previous_interval = signal.setitimer(signal.ITIMER_REAL, self.seconds)
        if previous_delay and previous_delay < self.seconds:
            # An enclosing timer is due first: keep its deadline.
            signal.setitimer(signal.ITIMER_REAL, previous_delay)
        self._previous_delay = previous_delay

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if not self._outer_fired:
            signal.signal(signal.SIGALRM, self._previous_handler)
            if self._previous_delay:
                remaining = self._previous_delay - (time.monotonic() - self._start)
                # Re-arm the enclosing timer; if its deadline already passed, fire ASAP.
                signal.setitimer(signal.ITIMER_REAL, max(remaining, 1e-6), self._previous_interval)
        if exc is not None and exc is self.error:
            _core.emit("timeout.expired", self.label, seconds=self.seconds)


# -- thread fallback: a small pool of idle daemon workers ---------------------------------

_WORKER_IDLE_SECONDS = 5.0  # an idle worker exits after this long (real time)
_MAX_IDLE_WORKERS = 8  # idle workers kept for reuse; extra ones exit when done


class _Job:
    """One call handed to a worker; ``done`` is released when it has finished."""

    __slots__ = (
        "abandoned",
        "args",
        "context",
        "done",
        "error",
        "finished",
        "func",
        "kwargs",
        "result",
    )

    def __init__(self, func: Callable[..., Any], args: Any, kwargs: Any) -> None:
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.context = contextvars.copy_context()  # per call, as with a fresh thread
        self.done = threading.Lock()
        self.done.acquire()
        self.result: Any = None
        self.error: BaseException | None = None
        self.abandoned = False  # the caller timed out and stopped waiting
        self.finished = 0.0  # `time.monotonic()` when the call returned (real time)

    def run(self) -> None:
        try:
            self.result = self.context.run(self.func, *self.args, **self.kwargs)
        except BaseException as exc:
            self.error = exc
        self.func = self.args = self.kwargs = None  # type: ignore[assignment]
        self.finished = time.monotonic()
        self.done.release()


class _WorkerPool:
    """Reusable daemon threads for the `timeout` thread fallback.

    A finished worker goes back to the idle list (up to ``_MAX_IDLE_WORKERS``); a worker
    whose call timed out is abandoned — it exits when that call eventually returns and
    is never reused. An idle worker exits after ``_WORKER_IDLE_SECONDS`` without work.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.idle: list[queue.SimpleQueue[_Job]] = []  # inboxes of idle workers (LIFO)
        self.count = 0

    def submit(self, job: _Job) -> None:
        with self.lock:
            inbox = self.idle.pop() if self.idle else None
            if inbox is None:
                self.count += 1
                number = self.count
        if inbox is not None:
            inbox.put(job)
            return
        inbox = queue.SimpleQueue()
        inbox.put(job)
        threading.Thread(
            target=self._work, args=(inbox,), name=f"timeout-worker-{number}", daemon=True
        ).start()

    def _work(self, inbox: queue.SimpleQueue[_Job]) -> None:
        while True:
            try:
                job = inbox.get(timeout=_WORKER_IDLE_SECONDS)
            except queue.Empty:
                with self.lock:
                    if inbox in self.idle:
                        self.idle.remove(inbox)
                        return
                continue  # claimed just as we gave up waiting: the job is on its way
            job.run()
            with self.lock:
                if job.abandoned or len(self.idle) >= _MAX_IDLE_WORKERS:
                    return
                self.idle.append(inbox)
            del job

    def abandon(self, job: _Job) -> None:
        with self.lock:
            job.abandoned = True

    def reset(self) -> None:
        """After `os.fork`: the parent's workers do not exist in the child."""
        self.lock = threading.Lock()
        self.idle = []


_workers = _WorkerPool()
if hasattr(os, "register_at_fork"):  # pragma: no branch - absent on Windows only
    os.register_at_fork(after_in_child=_workers.reset)


def _call_in_worker(
    func: Callable[P, R], label: str, seconds: float, *args: P.args, **kwargs: P.kwargs
) -> R:
    """Run ``func`` in a pooled daemon thread and stop waiting for it after ``seconds``."""
    job = _Job(func, args, kwargs)
    # Real time, like the wait itself: a fake clock advanced by sleeps inside the call
    # (e.g. a retry) must not turn a quick call into a late one.
    start = time.monotonic()
    _workers.submit(job)
    if not job.done.acquire(timeout=seconds):
        _workers.abandon(job)
        raise _expired(label, seconds) from None
    if job.finished - start > seconds:
        # The wait overslept (seen with free-threaded builds) and the call finished
        # late: it still ran over its limit.
        raise _expired(label, seconds) from None
    error = job.error
    if error is not None:
        job.error = None
        try:
            raise error
        finally:
            del error
    return cast(R, job.result)


if sys.version_info >= (3, 11):
    _async_timer = asyncio.timeout

else:  # pragma: no cover - exercised by the Python 3.10 CI job

    class _CancelTimer:
        """`asyncio.timeout` stand-in for 3.10: cancel the task at the deadline.

        Like `asyncio.timeout`, the cancellation that it caused is turned into a
        ``TimeoutError`` on exit; any other cancellation propagates unchanged.
        """

        def __init__(self, seconds: float) -> None:
            self._seconds = seconds
            self._fired = False

        def expired(self) -> bool:
            return self._fired

        async def __aenter__(self) -> _CancelTimer:
            task = asyncio.current_task()
            if task is None:
                raise RuntimeError("timeout() must be used inside a task")

            def fire() -> None:
                self._fired = True
                task.cancel()

            self._handle = asyncio.get_running_loop().call_later(self._seconds, fire)
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            self._handle.cancel()
            if self._fired and exc_type is asyncio.CancelledError:
                raise TimeoutError from exc

    _async_timer = _CancelTimer


class _GraceTimer:
    """Cancel the current task at the deadline, and again after ``grace`` seconds.

    The first cancellation lets the block run its ``finally`` / ``except CancelledError``
    cleanup; if the cleanup is still awaiting when the grace period ends, a second
    cancellation interrupts it (with a warning). Only cancellations caused by this timer
    are turned into ``TimeoutError``.
    """

    def __init__(self, seconds: float, grace: float, label: str) -> None:
        self._seconds = seconds
        self._grace = grace
        self._label = label
        self._fired = False
        self._cancels = 0
        self._handles: list[asyncio.TimerHandle] = []

    def expired(self) -> bool:
        return self._fired

    async def __aenter__(self) -> _GraceTimer:
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - asyncio always runs coroutines in a task
            raise RuntimeError("timeout() must be used inside a task")
        loop = asyncio.get_running_loop()

        def second() -> None:
            self._cancels += 1
            _core.get_logger(None).warning(
                "%s: cleanup still running %ss after the timeout, cancelling it again",
                self._label,
                self._grace,
            )
            task.cancel()

        def fire() -> None:
            self._fired = True
            self._cancels += 1
            task.cancel()
            self._handles.append(loop.call_later(self._grace, second))

        self._task = task
        self._handles.append(loop.call_later(self._seconds, fire))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        for handle in self._handles:
            handle.cancel()
        if self._fired and hasattr(self._task, "uncancel"):
            for _ in range(self._cancels):
                if self._task.uncancel() == 0:
                    break
        if self._fired and exc_type is asyncio.CancelledError:
            raise TimeoutError from exc


async def _await_with_grace(awaitable: Awaitable[T], label: str, seconds: float, grace: float) -> T:
    """Run ``awaitable`` as a child task; on expiry cancel it and wait ``grace`` for cleanup."""
    task = asyncio.ensure_future(awaitable)
    try:
        await asyncio.wait({task}, timeout=seconds)
    except BaseException:
        task.cancel()  # we were cancelled from outside: do not leave the child running
        raise
    if task.done():
        return task.result()
    task.cancel()
    try:
        await asyncio.wait({task}, timeout=grace)
    except BaseException:
        raise
    if not task.done():
        _core.get_logger(None).warning(
            "%s: still running %ss after the timeout; abandoning it", label, grace
        )
        task.add_done_callback(_consume_result)
    else:
        _consume_result(task)
    raise _expired(label, seconds)


def _consume_result(task: asyncio.Future[Any]) -> None:
    """Mark an abandoned task's outcome as retrieved so asyncio does not log it."""
    if not task.cancelled():
        task.exception()


async def _await_with_deadline(awaitable: Awaitable[T], label: str, seconds: float) -> T:
    """Await under a timer; convert only *our* expiry to TimeoutExpired."""
    timer = _async_timer(seconds)
    try:
        async with timer:
            return await awaitable
    except TimeoutError as exc:
        if not timer.expired():
            raise  # raised by the wrapped function itself: propagate unchanged
        raise _expired(label, seconds) from exc


class Timeout:
    """A time limit usable as a decorator, `with` block or `async with` block.

    Created by `timeout`; see there for the strategies and caveats. One instance can
    decorate several functions and be entered several times (also concurrently from
    different tasks); every use gets its own timer.

    Attributes:
        seconds: The configured limit.
        use_signals: The configured sync strategy (`None` means automatic).
        cleanup_grace: Seconds async code may spend cleaning up after the limit (`None`: no
            separate bound).
    """

    def __init__(
        self,
        seconds: float,
        *,
        use_signals: bool | None = None,
        cleanup_grace: float | None = None,
    ) -> None:
        if seconds <= 0:
            raise ValueError(f"timeout seconds must be positive, got {seconds!r}")
        if cleanup_grace is not None and cleanup_grace < 0:
            raise ValueError(f"cleanup_grace must be >= 0, got {cleanup_grace!r}")
        self.cleanup_grace = cleanup_grace
        if use_signals and not _HAS_SETITIMER:
            raise RuntimeError("use_signals=True needs signal.setitimer, unavailable here")
        self.seconds = seconds
        self.use_signals = use_signals
        self._alarms: list[_Alarm] = []
        self._timers: dict[asyncio.Task[Any], list[Any]] = {}

    def _limit(self, label: str) -> float:
        """The limit for a call starting now: ``seconds`` clamped to the deadline."""
        left = _deadline.remaining()
        if left is None or left >= self.seconds:
            return self.seconds
        if left <= 0:
            _core.emit("timeout.expired", label, seconds=0.0)
            raise TimeoutExpired(f"{label} not started: the deadline has passed", seconds=0.0)
        return left

    # -- decorator ----------------------------------------------------------------------

    def __call__(self, func: F, /) -> F:
        """Apply the limit to every call of ``func`` (sync or ``async def``)."""
        label = _core.qualname(func)
        use_signals = self.use_signals
        grace = self.cleanup_grace

        if grace is not None and not _core.is_async(func):
            raise TypeError(
                f"timeout(cleanup_grace=...) only supports async def functions; {label} is "
                "synchronous (signals and worker threads cannot give cleanup a bounded grace)"
            )

        if _core.is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                seconds = self._limit(label)
                if grace is not None:
                    return await _await_with_grace(func(*args, **kwargs), label, seconds, grace)
                return await _await_with_deadline(func(*args, **kwargs), label, seconds)

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if use_signals and not _signals_usable():
                raise RuntimeError(
                    "use_signals=True: signal-based timeouts only work in the main thread"
                )
            seconds = self._limit(label)
            if use_signals or (use_signals is None and _signals_usable()):
                with _Alarm(label, seconds):
                    return func(*args, **kwargs)
            return _call_in_worker(func, label, seconds, *args, **kwargs)

        return cast(F, wrapper)

    # -- sync context manager -----------------------------------------------------------

    def __enter__(self) -> Timeout:
        if self.cleanup_grace is not None:
            raise TypeError("timeout(cleanup_grace=...) is only supported with `async with`")
        if self.use_signals is False or not _signals_usable():
            raise RuntimeError(
                "`with timeout(...)` needs SIGALRM (Unix) and the main thread, because a "
                "block of code cannot be moved to a worker thread; decorate a function "
                "with @timeout instead, or use `async with timeout(...)` in async code"
            )
        alarm = _Alarm(_BLOCK, self._limit(_BLOCK))
        alarm.__enter__()
        self._alarms.append(alarm)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._alarms.pop().__exit__(exc_type, exc, tb)

    # -- async context manager ----------------------------------------------------------

    async def __aenter__(self) -> Timeout:
        task = asyncio.current_task()
        if task is None:  # pragma: no cover - asyncio always runs coroutines in a task
            raise RuntimeError("`async with timeout(...)` must run inside an asyncio task")
        seconds = self._limit(_BLOCK)
        timer: Any = (
            _async_timer(seconds)
            if self.cleanup_grace is None
            else _GraceTimer(seconds, self.cleanup_grace, _BLOCK)
        )
        await timer.__aenter__()
        self._timers.setdefault(task, []).append((timer, seconds))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        stack = self._timers[task]
        timer, seconds = stack.pop()
        if not stack:
            del self._timers[task]
        try:
            await timer.__aexit__(exc_type, exc, tb)
        except TimeoutError as error:
            if not timer.expired():  # pragma: no cover - only our own expiry is converted
                raise
            raise _expired(_BLOCK, seconds) from error

    def __repr__(self) -> str:
        return (
            f"Timeout(seconds={self.seconds!r}, use_signals={self.use_signals!r}, "
            f"cleanup_grace={self.cleanup_grace!r})"
        )


def timeout(
    seconds: float, *, use_signals: bool | None = None, cleanup_grace: float | None = None
) -> Timeout:
    """Raise `TimeoutExpired` if a call or block runs longer than ``seconds``.

    ``TimeoutExpired`` subclasses the built-in ``TimeoutError``. The returned `Timeout`
    works three ways:

    * ``@timeout(5)`` decorates sync and ``async def`` functions.
    * ``async with timeout(5):`` limits a block in async code (`asyncio.timeout` on
      Python 3.11+, a cancellation timer on 3.10).
    * ``with timeout(5):`` limits a block in sync code, with signals only: it needs
      ``signal.setitimer`` (Unix) and the main thread, otherwise entering it raises
      ``RuntimeError`` (a block cannot be moved to a worker thread; decorate a function
      instead).

    How each kind of code is stopped:

    * ``async def`` code is cancelled at the deadline. Only the timeout's own expiry is
      turned into ``TimeoutExpired``: a ``TimeoutError`` raised by the wrapped code
      itself propagates unchanged.
    * Sync code, with signals (the default on Unix when called from the main thread):
      a ``SIGALRM`` timer from `signal.setitimer` interrupts the call. The previous
      ``SIGALRM`` handler and any already-armed timer are restored afterwards, so nested
      timeouts work.
    * Sync functions, without signals (Windows, non-main threads, or
      ``use_signals=False``): the call runs in a daemon worker thread and the caller stops
      waiting at the deadline. **This fallback cannot stop the work** — the function keeps
      running in the background until it returns on its own, and its result is discarded.
      Workers are pooled daemon threads: an idle one is reused for the next call (at most
      8 are kept idle, each exits after about 5 s without work), a worker whose call timed
      out is abandoned and never reused, and the caller's ``contextvars`` are copied into
      the worker on every call.

    Inside a `python_wrap.deadline.deadline` block the limit is clamped to the time the
    deadline has left; if the deadline has already passed, the call (or block) is not
    started and ``TimeoutExpired`` is raised at once. Every expiry emits the
    ``timeout.expired`` event.

    Warning:
        With the signal strategy, ``TimeoutExpired`` is raised *inside* the wrapped
        code, at whatever line it is executing when the alarm fires. A broad
        ``except Exception:`` (or ``except TimeoutError:``) in that code catches it like
        any other error; if the code then carries on, the time limit is silently lost and
        the call returns normally. ``TimeoutExpired`` deliberately stays an ``Exception``
        (it subclasses ``TimeoutError``), so re-raise it explicitly in such handlers::

            except TimeoutExpired:
                raise
            except Exception:
                ...

        The same applies to context managers that suppress exceptions and to
        ``finally`` blocks that ``return``. ``async def`` code has the analogous
        caveat for code that swallows ``asyncio.CancelledError``.

    Args:
        seconds: Time limit in seconds; must be positive.
        use_signals: ``True`` forces the signal strategy, ``False`` forces the thread
            fallback (and so rules out ``with timeout(...)``), ``None`` (default) picks
            signals when they are usable.
        cleanup_grace: Async only. Seconds the code may spend in its ``finally`` /
            ``except CancelledError`` cleanup after the limit is hit. As a decorator, the
            call runs as a child task: at the limit it is cancelled and given
            ``cleanup_grace`` more seconds to finish; then ``TimeoutExpired`` is raised
            (even if the cleanup finished), and a task still running is abandoned with a
            warning logged to the package logger (it keeps running until it ends on its own).
            As ``async with timeout(5, cleanup_grace=1)`` the block cannot be moved to
            another task, so after the grace period the cleanup is cancelled a second time
            (with a warning) rather than abandoned; cleanup that suppresses cancellation
            cannot be stopped. Sync functions and ``with`` blocks are not supported and
            raise ``TypeError`` (signals and worker threads cannot bound cleanup).
            ``None`` (default) keeps the plain behaviour. Real time: not affected by the
            test overrides.

    Returns:
        A `Timeout`: a decorator that preserves the wrapped function's type, and a sync
        and async context manager.

    Raises:
        ValueError: If ``seconds`` is not positive or ``cleanup_grace`` is negative.
        TypeError: If ``cleanup_grace`` is set and the target is a sync function or a
            plain ``with`` block.
        RuntimeError: If ``use_signals=True`` but ``signal.setitimer`` does not exist on
            this platform (at creation time); if ``use_signals=True`` and the call is not
            made from the main thread (at call time); or on entering ``with`` where
            signals cannot be used.
        TimeoutExpired: From the wrapped call or block, when the time limit is exceeded.

    Example:
        ```python
        import asyncio
        from python_wrap.concurrency import timeout
        from python_wrap.errors import TimeoutExpired

        @timeout(5)
        def fetch() -> str:
            return "done"

        assert fetch() == "done"

        async def main() -> None:
            try:
                async with timeout(0.01):
                    await asyncio.sleep(1)
            except TimeoutExpired as exc:
                assert exc.seconds == 0.01

        asyncio.run(main())
        ```
    """
    return Timeout(seconds, use_signals=use_signals, cleanup_grace=cleanup_grace)


# ---------------------------------------------------------------------------------------
# rate_limit
# ---------------------------------------------------------------------------------------

_SWEEP_EVERY = 1024

Limit = tuple[int, float]
"""One rate limit: ``(calls, period_seconds)``."""


def _limits(
    calls: int | None, period: float | None, limits: Iterable[Limit] | None
) -> tuple[Limit, ...]:
    """Validate and combine the ``calls``/``period`` pair and ``limits``."""
    result: list[Limit] = []
    if calls is not None or period is not None:
        if calls is None or period is None:
            raise TypeError("rate limit: give both calls and period")
        result.append((calls, period))
    if limits is not None:
        result.extend((c, p) for c, p in limits)
    if not result:
        raise TypeError("rate limit: give calls and period, or limits=[(calls, period), ...]")
    for c, p in result:
        if c < 1:
            raise ValueError(f"rate_limit calls must be >= 1, got {c!r}")
        if p <= 0:
            raise ValueError(f"rate_limit period must be positive, got {p!r}")
    return tuple(result)


def _describe(limits: tuple[Limit, ...]) -> str:
    return " and ".join(f"{c} calls per {p:g}s" for c, p in limits)


class _SlidingWindow:
    """Thread-safe sliding-window logs of call timestamps: per key, one deque per limit."""

    def __init__(self, limits: tuple[Limit, ...]) -> None:
        self.limits = limits
        self._lock = threading.Lock()
        self._buckets: dict[Hashable, tuple[deque[float], ...]] = {}
        self._since_sweep = 0

    def acquire(self, key: Hashable) -> float:
        """Record a call and return 0.0, or return how long to wait before retrying.

        A call is recorded only if *every* limit allows it.
        """
        with self._lock:
            # Read the clock under the lock: a call that queued for the lock must be
            # recorded at the time it is admitted, not the time it arrived.
            now = _core.monotonic()
            self._since_sweep += 1
            if self._since_sweep >= _SWEEP_EVERY:
                self._sweep(now)
            buckets = self._buckets.get(key)
            if buckets is None:
                buckets = self._buckets[key] = tuple(deque() for _ in self.limits)
            wait = 0.0
            for (calls, period), bucket in zip(self.limits, buckets, strict=True):
                horizon = now - period
                while bucket and bucket[0] <= horizon:
                    bucket.popleft()
                if len(bucket) >= calls:
                    wait = max(wait, bucket[0] + period - now)
            if wait > 0:
                return wait
            for bucket in buckets:
                bucket.append(now)
            return 0.0

    def admit(self, key: Hashable) -> None:
        """Record a call now even if a limit is full (for `fast_mode()`'s blocked calls)."""
        with self._lock:
            now = _core.monotonic()
            buckets = self._buckets.get(key)
            if buckets is None:
                buckets = self._buckets[key] = tuple(deque() for _ in self.limits)
            for bucket in buckets:
                bucket.append(now)

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._since_sweep = 0

    def _sweep(self, now: float) -> None:
        self._since_sweep = 0
        periods = [period for _, period in self.limits]

        def is_stale(buckets: tuple[deque[float], ...]) -> bool:
            return all(not b or b[-1] <= now - p for p, b in zip(periods, buckets, strict=True))

        stale = [k for k, buckets in self._buckets.items() if is_stale(buckets)]
        for k in stale:
            del self._buckets[k]


class RateLimiter:
    """A reusable sliding-window rate limit, shared by everything that uses it.

    Use one instance to give several functions or code blocks a single budget:

    * as a decorator (on sync or ``async def`` functions and methods);
    * as ``with limiter:`` / ``async with limiter:`` around a block;
    * through ``acquire()`` / ``try_acquire()`` / ``acquire_async()``.

    A call is allowed when fewer than ``calls`` calls started in the last ``period``
    seconds (a sliding-window log, so no ``2 * calls`` burst at a window boundary). With
    ``limits=[(5, 1.0), (600, 900.0)]`` every limit must allow the call. Rejected or
    still-waiting calls are not counted. Thread-safe; async callers wait with ``await``
    and never block the event loop. While ``python_wrap.testing`` disables rate limits,
    every call goes through and nothing is recorded. Under ``fast_mode()`` (``no_sleep``)
    a blocking call that would wait is let through at once and still recorded, so later
    non-blocking calls see the limit; under ``fake_clock()`` the wait advances the fake
    clock instead.

    Events: ``rate_limit.rejected(retry_after)`` for each rejected call and
    ``rate_limit.waited(seconds)`` after a call had to wait, reported under ``name``
    (or the decorated function's qualified name).

    Args:
        calls: Maximum number of calls per ``period`` (>= 1).
        period: Window length in seconds (> 0).
        limits: More ``(calls, period)`` pairs; may replace or add to ``calls``/``period``.
        block: What the decorator and ``with`` do when over the limit: ``False``
            (default) raises `RateLimitExceeded`, ``True`` waits for a slot.
        name: Label for error messages and events.

    Raises:
        TypeError: If no limit is given, or only one of ``calls`` and ``period``.
        ValueError: If a limit is out of range.

    Example:
        ```python
        from python_wrap.concurrency import RateLimiter

        github = RateLimiter(limits=[(4, 1.0), (5000, 3600.0)], name="github")

        @github
        def get_repo(name: str) -> str:
            return name

        @github
        def get_user(name: str) -> str:
            return name

        assert get_repo("a") == "a" and get_user("b") == "b"  # one shared budget
        with github:  # a plain block counts too
            pass
        assert github.try_acquire()
        assert not github.try_acquire()  # 4 calls in the last second
        ```
    """

    def __init__(
        self,
        calls: int | None = None,
        period: float | None = None,
        *,
        limits: Iterable[Limit] | None = None,
        block: bool = False,
        name: str | None = None,
    ) -> None:
        self.limits = _limits(calls, period, limits)
        self.block = block
        self.name = name
        self._window = _SlidingWindow(self.limits)

    def __repr__(self) -> str:
        return f"RateLimiter(name={self.name!r}, limits={list(self.limits)!r})"

    # -- core ---------------------------------------------------------------------------

    def _rejected(self, label: str, retry_after: float) -> RateLimitExceeded:
        _core.emit("rate_limit.rejected", label, retry_after=retry_after)
        return RateLimitExceeded(
            f"{label} rate limit of {_describe(self.limits)} exceeded; "
            f"retry after {retry_after:.3g}s",
            retry_after=retry_after,
        )

    def _gives_up(self, label: str, wait: float, give_up: float | None) -> bool:
        """True (after emitting the rejection) if ``wait`` overshoots ``give_up``."""
        if give_up is not None and _core.monotonic() + wait > give_up:
            _core.emit("rate_limit.rejected", label, retry_after=wait)
            return True
        return False

    def _take(
        self, key: Hashable, label: str, *, block: bool, timeout: float | None, strict: bool
    ) -> bool:
        """Take a slot in a thread. ``strict`` raises on rejection instead of returning False."""
        if _core.overrides.disable_rate_limits:
            return True
        wait = self._window.acquire(key)
        if wait <= 0:
            return True
        if not block:
            error = self._rejected(label, wait)
            if strict:
                raise error
            return False
        start = _core.monotonic()
        give_up = None if timeout is None else start + timeout
        while wait > 0:
            if self._gives_up(label, wait, give_up):
                return False
            _core.sleep(wait)
            if _core.overrides.disable_rate_limits:
                break
            wait = self._window.acquire(key)
            if wait > 0 and _core.overrides.no_sleep:
                # fast_mode() and the sleep did not move the clock: admit the call
                # instead of spinning (it still counts toward the limit).
                self._window.admit(key)
                break
        _core.emit("rate_limit.waited", label, seconds=_core.monotonic() - start)
        return True

    async def _atake(
        self, key: Hashable, label: str, *, block: bool, timeout: float | None, strict: bool
    ) -> bool:
        """`_take` for coroutines: waits with ``await``."""
        if _core.overrides.disable_rate_limits:
            return True
        wait = self._window.acquire(key)
        if wait <= 0:
            return True
        if not block:
            error = self._rejected(label, wait)
            if strict:
                raise error
            return False
        start = _core.monotonic()
        give_up = None if timeout is None else start + timeout
        while wait > 0:
            if self._gives_up(label, wait, give_up):
                return False
            await _core.async_sleep(wait)
            if _core.overrides.disable_rate_limits:
                break
            wait = self._window.acquire(key)
            if wait > 0 and _core.overrides.no_sleep:
                self._window.admit(key)  # as in `_take`: never spin under fast_mode()
                break
        _core.emit("rate_limit.waited", label, seconds=_core.monotonic() - start)
        return True

    def _label(self) -> str:
        return self.name or "rate_limiter"

    # -- explicit API -------------------------------------------------------------------

    def reset(self) -> None:
        """Forget every recorded call (for every key), as if the limiter were new.

        Meant for tests: share one `RateLimiter` (or reach a `rate_limit` limiter with
        `limiter_of`) and reset it between tests. Thread-safe; calls waiting for a slot
        re-check and may then go through.
        """
        self._window.clear()

    def try_acquire(self) -> bool:
        """Take a slot if one is free right now.

        Returns:
            ``True`` if the call may proceed (it is counted), ``False`` otherwise.
        """
        return self._take(None, self._label(), block=False, timeout=None, strict=False)

    def acquire(self, block: bool = True, timeout: float | None = None) -> bool:
        """Take a slot, waiting for one in this thread if ``block`` is true.

        Never call it from a coroutine; use `acquire_async` there.

        Args:
            block: Wait for a slot (``True``) or return at once (``False``).
            timeout: Give up after this many seconds (``None`` waits as long as needed).
                Slots only free up as time passes, so the call returns ``False`` right
                away when the next slot is further away than ``timeout``.

        Returns:
            ``True`` if a slot was taken, ``False`` if not.
        """
        return self._take(None, self._label(), block=block, timeout=timeout, strict=False)

    async def acquire_async(self, block: bool = True, timeout: float | None = None) -> bool:
        """`acquire` for async code: waits with ``await``, never blocking the loop.

        Args:
            block: Wait for a slot (``True``) or return at once (``False``).
            timeout: Give up after this many seconds (``None`` waits as long as needed).

        Returns:
            ``True`` if a slot was taken, ``False`` if not.
        """
        return await self._atake(None, self._label(), block=block, timeout=timeout, strict=False)

    def __enter__(self) -> RateLimiter:
        self._take(None, self._label(), block=self.block, timeout=None, strict=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def __aenter__(self) -> RateLimiter:
        await self._atake(None, self._label(), block=self.block, timeout=None, strict=True)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    # -- decorator ----------------------------------------------------------------------

    def __call__(self, func: F, /) -> F:
        """Count every call of ``func`` against this limiter (sync or ``async def``)."""
        return self._wrap(func, None)

    def _wrap(self, func: F, key: Callable[..., Hashable] | None) -> F:
        label = self.name or _core.qualname(func)
        block = self.block

        if _core.is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                k = None if key is None else key(*args, **kwargs)
                await self._atake(k, label, block=block, timeout=None, strict=True)
                return await func(*args, **kwargs)

            async_wrapper.__rate_limiter__ = self  # type: ignore[attr-defined]
            return cast(F, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            k = None if key is None else key(*args, **kwargs)
            self._take(k, label, block=block, timeout=None, strict=True)
            return func(*args, **kwargs)

        wrapper.__rate_limiter__ = self  # type: ignore[attr-defined]
        return cast(F, wrapper)


def limiter_of(func: Callable[..., Any], /) -> RateLimiter | None:
    """The `RateLimiter` behind a function decorated with `rate_limit` (or a limiter).

    `rate_limit` keeps the decorated function's exact type, so its limiter is reached
    through this function rather than an attribute. Decorators applied on top with
    `functools.wraps` keep it reachable; bound methods work too.

    Args:
        func: A function decorated with `rate_limit` or with a `RateLimiter`.

    Returns:
        Its limiter (the outermost one, if several), or `None` if it has none.

    Example:
        ```python
        from python_wrap.concurrency import limiter_of, rate_limit

        @rate_limit(1, 60.0)
        def ping() -> str:
            return "pong"

        assert ping() == "pong"
        limiter = limiter_of(ping)
        assert limiter is not None and not limiter.try_acquire()
        limiter.reset()  # e.g. between tests
        assert ping() == "pong"
        ```
    """
    limiter = getattr(func, "__rate_limiter__", None)
    return limiter if isinstance(limiter, RateLimiter) else None


def rate_limit(
    calls: int | None = None,
    period: float | None = None,
    *,
    block: bool = False,
    key: Callable[..., Hashable] | None = None,
    limits: Iterable[Limit] | None = None,
) -> Callable[[F], F]:
    """Allow at most ``calls`` calls per ``period`` seconds.

    Uses a sliding-window log: a call is allowed when fewer than ``calls`` calls started
    in the last ``period`` seconds. Unlike a fixed window, this never lets a burst of
    ``2 * calls`` through at a window boundary. Rejected (or waiting) calls are not
    counted. The limiter is thread-safe and is shared by every caller of the decorated
    function — including every instance, for methods — unless ``key`` splits it.

    Each decorated function gets its own `RateLimiter`, reachable with
    ``limiter_of(func)`` (e.g. to ``.reset()`` it between tests). To share one budget
    between several functions, create a `RateLimiter` and use it as the decorator. Pass
    ``limits=[(5, 1.0), (600, 900.0)]`` to enforce several limits at once (all must
    allow a call). While ``python_wrap.testing`` disables rate limits, every call goes
    through; under ``fast_mode()`` a ``block=True`` call that would wait goes through at
    once (and is counted). Emits ``rate_limit.rejected`` and ``rate_limit.waited``.

    Args:
        calls: Maximum number of calls per window; at least 1.
        period: Window length in seconds; must be positive.
        block: If ``False`` (default) an over-limit call raises ``RateLimitExceeded``.
            If ``True`` it waits until allowed (``async def`` functions wait with
            ``await``, never blocking the event loop).
        key: Optional function called with the call's arguments (including ``self`` for
            methods) that returns a hashable bucket key, e.g. a user id. Each key gets
            its own limit.
        limits: Further ``(calls, period)`` pairs, alone or on top of ``calls``/``period``.

    Returns:
        A decorator that preserves the wrapped function's signature.

    Raises:
        TypeError: If no limit is given, or only one of ``calls`` and ``period``.
        ValueError: If ``calls < 1`` or ``period <= 0`` (for any limit).
        RateLimitExceeded: From the wrapped call when over the limit and ``block`` is
            false. Its ``retry_after`` attribute says how many seconds until a slot frees.

    Example:
        >>> @rate_limit(2, 1.0, key=lambda user, *_: user)
        ... def send(user: str, text: str) -> None: ...
        >>> @rate_limit(limits=[(5, 1.0), (600, 900.0)])
        ... def strava_call() -> None: ...
    """
    all_limits = _limits(calls, period, limits)

    def decorate(func: F) -> F:
        return RateLimiter(limits=all_limits, block=block)._wrap(func, key)

    return decorate


# ---------------------------------------------------------------------------------------
# run_in_thread
# ---------------------------------------------------------------------------------------


class _RunInThreadDecorator(Protocol):
    @overload
    def __call__(
        self, func: Callable[P, Coroutine[Any, Any, T]], /
    ) -> Callable[P, concurrent.futures.Future[T]]: ...
    @overload
    def __call__(self, func: Callable[P, T], /) -> Callable[P, concurrent.futures.Future[T]]: ...


@overload
def run_in_thread(
    func: Callable[P, Coroutine[Any, Any, T]], /
) -> Callable[P, concurrent.futures.Future[T]]: ...
@overload
def run_in_thread(func: Callable[P, T], /) -> Callable[P, concurrent.futures.Future[T]]: ...
@overload
def run_in_thread(
    func: None = None, /, *, daemon: bool = False, name: str | None = None
) -> _RunInThreadDecorator: ...
def run_in_thread(
    func: Callable[..., Any] | None = None,
    /,
    *,
    daemon: bool = False,
    name: str | None = None,
) -> Any:
    """Run each call in a new thread and return a `concurrent.futures.Future`.

    The future resolves to the function's return value, or raises its exception from
    ``future.result()``. For ``async def`` functions the coroutine is run with
    `asyncio.run` inside the new thread (so it gets its own event loop), and the
    wrapper itself is a plain function returning a ``Future``. The caller's
    ``contextvars`` are copied into the thread.

    Changed in 0.2.0: the wrapper used to return the ``threading.Thread``, with no way to
    get the result. Call ``.result()`` on the returned future to wait (the equivalent of
    the old ``thread.join()``).

    Args:
        func: The function to wrap (bare ``@run_in_thread`` form).
        daemon: Start daemon threads, which do not keep the interpreter alive.
        name: Thread name; defaults to ``run_in_thread-<qualname>``.

    Returns:
        The wrapped function, or a decorator when called with options only.

    Example:
        >>> @run_in_thread
        ... def add(a: int, b: int) -> int:
        ...     return a + b
        >>> add(1, 2).result()
        3
    """

    def decorate(f: Callable[..., Any]) -> Callable[..., concurrent.futures.Future[Any]]:
        thread_name = name or f"run_in_thread-{_core.qualname(f)}"
        is_coro = _core.is_async(f)

        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> concurrent.futures.Future[Any]:
            future: concurrent.futures.Future[Any] = concurrent.futures.Future()
            context = contextvars.copy_context()

            def target() -> Any:
                if is_coro:
                    return asyncio.run(f(*args, **kwargs))
                return f(*args, **kwargs)

            def runner() -> None:
                if not future.set_running_or_notify_cancel():
                    return
                try:
                    future.set_result(context.run(target))
                except BaseException as exc:
                    future.set_exception(exc)

            threading.Thread(target=runner, name=thread_name, daemon=daemon).start()
            return future

        return wrapper

    return decorate if func is None else decorate(func)


# ---------------------------------------------------------------------------------------
# throttle
# ---------------------------------------------------------------------------------------


class _ThrottleDecorator(Protocol):
    @overload
    def __call__(
        self, func: Callable[P, Coroutine[Any, Any, T]], /
    ) -> Callable[P, Coroutine[Any, Any, T | None]]: ...
    @overload
    def __call__(self, func: Callable[P, T], /) -> Callable[P, T | None]: ...


def throttle(interval: float) -> _ThrottleDecorator:
    """Run the function at most once per ``interval`` seconds.

    The first call runs and opens a window of ``interval`` seconds (measured from when
    the call started). Calls inside the window do not run; they return the result of the
    most recent run that *returned* (``None`` if none has returned yet). A call that
    raises propagates its exception to its own caller and still uses up its window, but
    does not replace the remembered result: calls skipped inside that window get the
    previous successful result (or ``None``), not the error. Thread-safe; the function
    itself runs outside the lock, so a slow call does not block skipped callers.
    ``async def`` functions get the same semantics with an ``async`` wrapper.

    Args:
        interval: Minimum seconds between runs; must be positive.

    Returns:
        A decorator; the wrapped function returns ``R | None``.

    Raises:
        ValueError: If ``interval`` is not positive.

    Example:
        >>> @throttle(1.0)
        ... def refresh() -> str:
        ...     return "fresh"
    """
    if interval <= 0:
        raise ValueError(f"throttle interval must be positive, got {interval!r}")

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        lock = threading.Lock()
        last_start: float | None = None
        last_result: Any = None

        name = _core.qualname(func)

        def should_run() -> tuple[bool, Any]:
            nonlocal last_start
            if _core.overrides.disable_rate_limits:
                return True, None
            now = _core.monotonic()
            with lock:
                if last_start is None or now - last_start >= interval:
                    last_start = now
                    return True, None
                previous = last_result
            _core.emit("throttle.skipped", name)
            return False, previous

        def store(result: Any) -> Any:
            nonlocal last_result
            with lock:
                last_result = result
            return result

        if _core.is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                run, previous = should_run()
                if not run:
                    return previous
                return store(await func(*args, **kwargs))

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            run, previous = should_run()
            if not run:
                return previous
            return store(func(*args, **kwargs))

        return wrapper

    return cast(_ThrottleDecorator, decorate)


# ---------------------------------------------------------------------------------------
# debounce
# ---------------------------------------------------------------------------------------


class Debounced(Protocol[P, R_co]):
    """A debounced sync function: calls return ``None``; the real call happens later.

    Binds ``self`` and ``cls`` like `python_wrap.caching.Memoized`, with the same typing
    limitations.
    """

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> None: ...

    @overload
    def __get__(
        self: Debounced[Concatenate[type[T], P2], R_co], instance: None, owner: type[T]
    ) -> _DebouncedOnClass[P, P2, R_co]: ...
    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Debounced[P, R_co]: ...
    @overload
    def __get__(
        self: Debounced[Concatenate[Any, P2], R_co], instance: object, owner: type | None = None
    ) -> Debounced[P2, R_co]: ...

    def cancel(self) -> None:
        """Drop the pending call, if any."""

    def flush(self) -> R_co | None:
        """Run the pending call now and return its result (``None`` if nothing pending)."""


class _DebouncedOnClass(Debounced[P, R_co], Protocol[P, P2, R_co]):
    """`Debounced` read from a class: accepts the call with and without the first argument.

    Returned for class access (``Cls.attr``) when the first parameter could be ``cls``.
    pyright only picks it under ``@classmethod``; mypy cannot tell a classmethod from an
    instance or static method there, so both call forms are accepted.
    """

    @overload
    def __call__(self, *args: P2.args, **kwargs: P2.kwargs) -> None: ...
    @overload
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> None: ...


class AsyncDebounced(Protocol[P, R_co]):
    """A debounced ``async def`` function; see `debounce` for the await semantics.

    Binds ``self`` and ``cls`` like `python_wrap.caching.Memoized`, with the same typing
    limitations.
    """

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Coroutine[Any, Any, R_co]: ...

    @overload
    def __get__(
        self: AsyncDebounced[Concatenate[type[T], P2], R_co], instance: None, owner: type[T]
    ) -> _AsyncDebouncedOnClass[P, P2, R_co]: ...
    @overload
    def __get__(self, instance: None, owner: type | None = None) -> AsyncDebounced[P, R_co]: ...
    @overload
    def __get__(
        self: AsyncDebounced[Concatenate[Any, P2], R_co],
        instance: object,
        owner: type | None = None,
    ) -> AsyncDebounced[P2, R_co]: ...

    def cancel(self) -> None:
        """Drop the pending call; callers awaiting it get ``CancelledError``."""

    def flush(self) -> Coroutine[Any, Any, R_co | None]:
        """Run the pending call now; awaiting returns its result (``None`` if none)."""


class _AsyncDebouncedOnClass(AsyncDebounced[P, R_co], Protocol[P, P2, R_co]):
    """`AsyncDebounced` read from a class: accepts the call with and without the first argument.

    Returned for class access (``Cls.attr``) when the first parameter could be ``cls``.
    pyright only picks it under ``@classmethod``; mypy cannot tell a classmethod from an
    instance or static method there, so both call forms are accepted.
    """

    @overload
    def __call__(self, *args: P2.args, **kwargs: P2.kwargs) -> Coroutine[Any, Any, R_co]: ...
    @overload
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Coroutine[Any, Any, R_co]: ...


class _DebounceDecorator(Protocol):
    @overload
    def __call__(  # type: ignore[overload-overlap]
        self, func: Callable[P, Coroutine[Any, Any, T]], /
    ) -> AsyncDebounced[P, T]: ...
    @overload
    def __call__(self, func: Callable[P, T], /) -> Debounced[P, T]: ...


_Pending = tuple[tuple[Any, ...], dict[str, Any]]


def _debounce_sync(func: Callable[..., Any], wait: float) -> Callable[..., None]:
    lock = threading.Lock()
    timer: threading.Timer | None = None
    pending: _Pending | None = None

    def take() -> _Pending | None:
        nonlocal timer, pending
        if timer is not None:
            timer.cancel()
        call, timer, pending = pending, None, None
        return call

    def fire(me: list[threading.Timer]) -> None:
        with lock:
            if timer is not me[0]:  # superseded or cancelled meanwhile
                return
            call = take()
        if call is not None:
            func(*call[0], **call[1])

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        nonlocal timer, pending
        with lock:
            take()
            me: list[threading.Timer] = []
            timer = threading.Timer(wait, fire, args=(me,))
            me.append(timer)
            timer.daemon = True
            pending = (args, kwargs)
            timer.start()

    def cancel() -> None:
        with lock:
            take()

    def flush() -> Any:
        with lock:
            call = take()
        return None if call is None else func(*call[0], **call[1])

    wrapper.cancel = cancel  # type: ignore[attr-defined]
    wrapper.flush = flush  # type: ignore[attr-defined]
    return wrapper


class _AsyncBurst:
    """One burst of async debounced calls: the latest arguments and a shared outcome."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.outcome: asyncio.Future[Any] = loop.create_future()
        self.timer: asyncio.Task[None] | None = None
        self.call: _Pending | None = None


def _debounce_async(func: Callable[..., Awaitable[Any]], wait: float) -> Callable[..., Any]:
    bursts: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _AsyncBurst] = (
        weakref.WeakKeyDictionary()
    )
    # Several loops (in several threads) share `bursts`; each burst itself is only ever
    # touched from its own loop's thread.
    bursts_lock = threading.Lock()

    async def run(burst: _AsyncBurst) -> Any:
        loop = asyncio.get_running_loop()
        with bursts_lock:
            if bursts.get(loop) is burst:
                del bursts[loop]
        call = burst.call
        if call is None or burst.outcome.done():
            return None
        try:
            result = await func(*call[0], **call[1])
        except asyncio.CancelledError:
            burst.outcome.cancel()
            raise
        except BaseException as exc:
            if not burst.outcome.done():
                burst.outcome.set_exception(exc)
                burst.outcome.exception()  # mark retrieved: callers may all be gone
            raise
        if not burst.outcome.done():
            burst.outcome.set_result(result)
        return result

    async def sleep_then_run(burst: _AsyncBurst) -> None:
        await asyncio.sleep(wait)  # real time, like the sync `threading.Timer`
        burst.timer = None  # past the cancellation point: run even if called again now
        with contextlib.suppress(Exception):  # delivered to callers via burst.outcome
            await run(burst)

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        with bursts_lock:
            burst = bursts.get(loop)
            if burst is None:
                burst = bursts[loop] = _AsyncBurst(loop)
        if burst.timer is not None:
            burst.timer.cancel()
        burst.call = (args, kwargs)
        burst.timer = loop.create_task(sleep_then_run(burst))
        return await asyncio.shield(burst.outcome)

    def cancel_burst(burst: _AsyncBurst) -> None:
        if burst.timer is not None:
            burst.timer.cancel()
        burst.outcome.cancel()

    def cancel() -> None:
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        with bursts_lock:
            if running is not None:
                taken = [(running, bursts.pop(running, None))]
            else:
                taken = [(lp, bursts.pop(lp)) for lp in list(bursts)]
        for lp, burst in taken:
            if burst is None:
                continue
            if lp.is_closed():
                continue
            if lp is running or not lp.is_running():
                cancel_burst(burst)
            else:
                # Another thread's loop: futures are not thread-safe, so hand the
                # cancellation to that loop (which also wakes it up).
                with contextlib.suppress(RuntimeError):  # closed meanwhile
                    lp.call_soon_threadsafe(cancel_burst, burst)

    async def flush() -> Any:
        with bursts_lock:
            burst = bursts.get(asyncio.get_running_loop())
        if burst is None:
            return None
        if burst.timer is not None:
            burst.timer.cancel()
            burst.timer = None
        return await run(burst)

    wrapper.cancel = cancel  # type: ignore[attr-defined]
    wrapper.flush = flush  # type: ignore[attr-defined]
    return wrapper


def debounce(wait: float) -> _DebounceDecorator:
    """Postpone running until ``wait`` seconds have passed since the last call.

    Each call restarts the countdown and replaces the pending arguments, so only the last
    call of a burst runs.

    * Sync functions: the countdown is a daemon `threading.Timer`, so the function
      runs in the timer's thread. The wrapper returns ``None`` immediately.
    * ``async def`` functions: a call schedules a task on the running loop (cancelling the
      previously scheduled one) and then waits for the burst to finish. **Every caller in
      a burst awaits the same outcome** — the result (or exception) of the final call —
      so superseded callers are not left hanging or cancelled. Calls must be awaited (or
      wrapped in a task) to take effect; a single task awaiting sequentially simply waits
      ``wait`` seconds per call. Bursts are tracked per event loop.

    The wrapper also has:

    * ``.cancel()`` — drop the pending call (async awaiters get ``CancelledError``).
      Thread-safe. For ``async def`` functions, inside a running loop it cancels that
      loop's pending call; from a thread with no running loop it cancels the pending
      call of every loop (handing the cancellation to each loop's own thread).
    * ``.flush()`` — run the pending call right now and return its result, or ``None``
      if nothing is pending. For ``async def`` functions it is a coroutine to ``await``.

    State belongs to the decorated function, so on a method it is shared by all
    instances: calls on different instances debounce each other.

    Args:
        wait: Quiet period in seconds; must be non-negative.

    Returns:
        A decorator producing a `Debounced` or `AsyncDebounced` wrapper.

    Raises:
        ValueError: If ``wait`` is negative.

    Example:
        >>> @debounce(0.3)
        ... def save(text: str) -> None: ...
        >>> save("a"); save("ab"); save.flush()  # runs once, with "ab"
    """
    if wait < 0:
        raise ValueError(f"debounce wait must be non-negative, got {wait!r}")

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        if _core.is_async(func):
            return _debounce_async(func, wait)
        return _debounce_sync(func, wait)

    return cast(_DebounceDecorator, decorate)


# ---------------------------------------------------------------------------------------
# singleflight
# ---------------------------------------------------------------------------------------


class _KwargsMark:
    """Private marker in keys built from keyword arguments (never equal to user data)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<kwargs>"


_KWARGS: Final = _KwargsMark()


def _default_key(*args: Any, **kwargs: Any) -> Hashable:
    # The marker keeps ``f(1, b=2)`` and ``f((1,), (("b", 2),))`` apart.
    return (args, _KWARGS, tuple(sorted(kwargs.items()))) if kwargs else args


class _Flights(Generic[T]):
    """Thread-side bookkeeping for singleflight: key -> (in-flight future, leader thread)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.inflight: dict[Hashable, tuple[concurrent.futures.Future[T], int]] = {}


class _AsyncFlight:
    """One in-flight async singleflight run: its task and the callers waiting for it."""

    __slots__ = ("task", "waiters")

    def __init__(self) -> None:
        self.task: asyncio.Task[None] | None = None
        self.waiters: list[asyncio.Future[Any]] = []

    def deliver(self, result: Any = None, exc: BaseException | None = None) -> None:
        """Hand the outcome to every caller still waiting (``exc=None`` means success)."""
        waiters, self.waiters = self.waiters, []
        for waiter in waiters:
            if waiter.done():
                continue
            if exc is None:
                waiter.set_result(result)
            elif isinstance(exc, asyncio.CancelledError):
                waiter.cancel()
            else:
                waiter.set_exception(exc)


def _start_task(
    loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, None]
) -> asyncio.Task[None]:
    """Start the shared run, eagerly where possible.

    On Python 3.12+ with no custom task factory the task starts eagerly: it runs right
    away, up to its first suspension, so a run that never suspends (an in-memory hit,
    say) has finished before the caller would have waited at all. Otherwise (3.10/3.11,
    or a task factory is installed, which is respected) it starts on the next loop
    iteration, as `loop.create_task` does.
    """
    if _EAGER_TASKS and loop.get_task_factory() is None:
        return asyncio.Task(coro, loop=loop, eager_start=True)  # type: ignore[call-arg,unused-ignore]
    return loop.create_task(coro)


_EAGER_TASKS = sys.version_info >= (3, 12)


async def _await_flight(flight: _AsyncFlight, waiter: asyncio.Future[T]) -> T:
    """Wait for a flight's outcome; a cancelled caller detaches, the run carries on.

    The run resolves ``waiter`` itself (no done-callback relay), so an uncontended call
    costs one loop iteration for the run plus the wake-up, and none at all when the
    task ran eagerly to completion.
    """
    try:
        return await waiter
    except asyncio.CancelledError:
        if waiter.done() and not waiter.cancelled():
            waiter.exception()  # outcome arrived just as we were cancelled: retrieved
        else:
            with contextlib.suppress(ValueError):
                flight.waiters.remove(waiter)
        raise


@overload
def singleflight(func: F, /) -> F: ...
@overload
def singleflight(
    func: None = None, /, *, key: Callable[..., Hashable] | None = None
) -> Callable[[F], F]: ...
def singleflight(
    func: Callable[..., Any] | None = None,
    /,
    *,
    key: Callable[..., Hashable] | None = None,
) -> Any:
    """Let concurrent calls with the same arguments share a single execution.

    While a call for a given key is running, further calls with that key do not start
    the function again; they wait and receive the same result, or the same exception.
    Once it finishes, the next call runs fresh — this de-duplicates work in flight, it
    is not a cache.

    Sync functions de-duplicate across threads. ``async def`` functions de-duplicate
    across tasks on the same event loop: the work runs in a shared task, so cancelling
    one waiting caller does not cancel it for the others.

    A re-entrant call — the function calling itself with the same key, on the leader's
    own thread (sync) or from inside the shared task (async) — cannot wait for its own
    result, so it runs the function directly, as if it were not decorated.

    Args:
        func: The function to wrap (bare ``@singleflight`` form).
        key: Function of the call's arguments returning a hashable key. Defaults to the
            positional arguments plus sorted keyword arguments (including ``self`` for
            methods), which must then be hashable.

    Returns:
        The wrapped function, or a decorator when called with options only.

    Raises:
        TypeError: If the default key is used and an argument is unhashable.

    Example:
        >>> @singleflight
        ... def load_config(path: str) -> str:
        ...     return open(path).read()
    """
    key_func = key or _default_key

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        if _core.is_async(f):
            async_f = cast(Callable[P, Awaitable[Any]], f)
            flights_by_loop: weakref.WeakKeyDictionary[
                asyncio.AbstractEventLoop, dict[Hashable, _AsyncFlight]
            ] = weakref.WeakKeyDictionary()
            loops_lock = threading.Lock()  # loops in several threads share the mapping

            @functools.wraps(f)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                k = key_func(*args, **kwargs)
                loop = asyncio.get_running_loop()
                inflight = flights_by_loop.get(loop)
                if inflight is None:
                    with loops_lock:
                        inflight = flights_by_loop.setdefault(loop, {})
                flight = inflight.get(k)
                if flight is not None and flight.task is asyncio.current_task():
                    # Re-entrant call from inside the shared task: waiting for ourselves
                    # would deadlock, so run the function directly.
                    return await async_f(*args, **kwargs)
                waiter: asyncio.Future[Any] = loop.create_future()
                if flight is not None:
                    flight.waiters.append(waiter)
                    return await _await_flight(flight, waiter)

                new = inflight[k] = _AsyncFlight()
                new.waiters.append(waiter)  # before the task starts: it may run eagerly

                async def run() -> None:
                    new.task = asyncio.current_task()
                    try:
                        result = await async_f(*args, **kwargs)
                    except BaseException as exc:
                        if inflight.get(k) is new:
                            del inflight[k]
                        new.deliver(exc=exc)
                        if not isinstance(exc, Exception):
                            raise  # CancelledError, KeyboardInterrupt, SystemExit
                        # Delivered to the callers; the task itself ends quietly, so an
                        # orphaned failure is never logged as "never retrieved".
                    else:
                        if inflight.get(k) is new:
                            del inflight[k]
                        new.deliver(result)

                # Keep a strong reference: the event loop only holds tasks weakly.
                new.task = _start_task(loop, run())
                return await _await_flight(new, waiter)

            return cast(Callable[P, R], async_wrapper)

        flights: _Flights[R] = _Flights()

        @functools.wraps(f)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            k = key_func(*args, **kwargs)
            me = threading.get_ident()
            with flights.lock:
                entry = flights.inflight.get(k)
                if entry is None:
                    future: concurrent.futures.Future[R] = concurrent.futures.Future()
                    flights.inflight[k] = (future, me)
                    leader = True
                else:
                    future, owner = entry
                    leader = False
            if not leader:
                if owner == me:
                    # Re-entrant call on the leader's own thread: waiting for our own
                    # result would deadlock, so run the function directly.
                    return f(*args, **kwargs)
                return future.result()
            try:
                result = f(*args, **kwargs)
            except BaseException as exc:
                with flights.lock:
                    del flights.inflight[k]
                future.set_exception(exc)
                raise
            with flights.lock:
                del flights.inflight[k]
            future.set_result(result)
            return result

        return wrapper

    return decorate if func is None else decorate(func)
