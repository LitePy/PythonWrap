"""`timeout`, cancellation tokens and the worker pool behind thread timeouts."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import os
import queue
import signal
import sys
import threading
import time
import warnings
from collections.abc import Awaitable, Callable
from types import FrameType, TracebackType
from typing import (
    Any,
    ParamSpec,
    TypeVar,
    cast,
)

from python_wrap import _core
from python_wrap import deadline as _deadline
from python_wrap._core import F, P, R
from python_wrap.errors import TimeoutExpired

T = TypeVar("T")
R_co = TypeVar("R_co", covariant=True)
P2 = ParamSpec("P2")
S = TypeVar("S")

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


class SignalTimerConflictWarning(RuntimeWarning):
    """`timeout` is about to take over ``SIGALRM`` that something else already uses.

    Signal timers are process-wide: a non-default ``SIGALRM`` handler or an armed
    ``ITIMER_REAL`` timer belongs to other code (a profiler, a watchdog, another
    library). `timeout` restores both afterwards, but while it runs the other code's
    alarm may be delayed or fire into the timeout's handler. Pass ``use_signals=False`` to
    use the worker-thread fallback, or silence it with `warnings.filterwarnings`.
    """


class CancelToken:
    """A flag a sync function can poll to learn that its `timeout` has fired.

    `timeout` cannot stop a sync function off the main thread (or where ``SIGALRM`` is
    missing): the caller gets `TimeoutExpired` but the function keeps running. Inside a
    `timeout`-wrapped sync function the current token is cancelled when the limit is hit,
    so the function can stop itself at safe points with `checkpoint` or `cancel_requested`.
    A token also reports cancelled when the token of an enclosing `timeout` is.

    Attributes:
        cancelled: True once `cancel` was called (on this token or an enclosing one).

    Example:
        ```python
        from python_wrap import checkpoint, timeout

        @timeout(5)
        def import_rows(rows: list[int]) -> int:
            done = 0
            for _ in rows:
                checkpoint()  # raises TimeoutExpired once the 5 s are over
                done += 1
            return done
        ```
    """

    __slots__ = ("_event", "_parent", "label", "seconds")

    def __init__(
        self, label: str = "", seconds: float = 0.0, parent: CancelToken | None = None
    ) -> None:
        self.label = label
        self.seconds = seconds
        self._parent = parent
        self._event = threading.Event()

    def cancel(self) -> None:
        """Mark the token cancelled (what `timeout` does when its limit is hit)."""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        token: CancelToken | None = self
        while token is not None:
            if token._event.is_set():
                return True
            token = token._parent
        return False

    def wait(self, timeout: float | None = None) -> bool:
        """Sleep up to ``timeout`` seconds, waking early when cancelled; True if cancelled.

        Only this token's own cancellation wakes the sleep early; an enclosing token's
        cancellation is seen after the sleep.
        """
        self._event.wait(timeout)
        return self.cancelled

    def raise_if_cancelled(self) -> None:
        """Raise `TimeoutExpired` if the token is cancelled."""
        token: CancelToken | None = self
        while token is not None:
            if token._event.is_set():
                raise TimeoutExpired(
                    _timeout_message(token.label or _BLOCK, token.seconds), seconds=token.seconds
                )
            token = token._parent

    def __repr__(self) -> str:
        return f"CancelToken(cancelled={self.cancelled!r})"


_current_token: contextvars.ContextVar[CancelToken | None] = contextvars.ContextVar(
    "python_wrap_cancel_token", default=None
)


def current_token() -> CancelToken | None:
    """The `CancelToken` of the innermost sync `timeout` running here, or None."""
    return _current_token.get()


def cancel_requested() -> bool:
    """True if the enclosing sync `timeout` has fired (False outside any `timeout`).

    Poll it in long loops of a sync function to stop work that `timeout` cannot
    interrupt (worker-thread fallback, Windows). The worker-thread fallback copies the
    caller's context, so the token is visible there.
    """
    token = _current_token.get()
    return token is not None and token.cancelled


def checkpoint() -> None:
    """Raise `TimeoutExpired` if the enclosing sync `timeout` has fired; else do nothing.

    A cooperative stop point: call it between units of work so the function ends soon
    after the limit instead of running on in the background.
    """
    token = _current_token.get()
    if token is not None:
        token.raise_if_cancelled()


def _foreign_alarm(previous_handler: Any, previous_delay: float) -> str | None:
    """Describe a ``SIGALRM`` user other than python-wrap, or None if there is none."""
    if getattr(previous_handler, "__python_wrap_alarm__", False):
        return None
    if previous_handler not in (signal.SIG_DFL, None):
        return f"a SIGALRM handler is already installed ({previous_handler!r})"
    if previous_delay:
        return f"an ITIMER_REAL timer is already armed ({previous_delay:g}s left)"
    return None


def foreign_alarm_state() -> str | None:
    """Describe an existing non-default ``SIGALRM`` handler or armed timer, if any.

    Used by `doctor` (PW010) and by `timeout` before it installs its own handler.
    Returns None where signals are unavailable or nothing foreign is in place.
    """
    if not _HAS_SETITIMER:
        return None
    try:
        handler = signal.getsignal(signal.SIGALRM)
        delay = signal.getitimer(signal.ITIMER_REAL)[0]
    except (ValueError, OSError):  # pragma: no cover - platform quirk
        return None
    return _foreign_alarm(handler, delay)


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
        self.token = CancelToken(label, seconds, _current_token.get())
        self._token_reset: contextvars.Token[CancelToken | None] | None = None
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
                self.token.cancel()
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

        handler.__python_wrap_alarm__ = True  # type: ignore[attr-defined]
        signal.signal(signal.SIGALRM, handler)
        previous_delay, self._previous_interval = signal.setitimer(signal.ITIMER_REAL, self.seconds)
        self._token_reset = _current_token.set(self.token)
        foreign = _foreign_alarm(previous_handler, previous_delay)
        if foreign is not None:
            warnings.warn(
                f"timeout({self.seconds:g}s) for {self.label} takes over SIGALRM but {foreign}; "
                "signal timers are process-wide, so the other alarm may be delayed or "
                "misrouted while this runs (use_signals=False avoids signals)",
                SignalTimerConflictWarning,
                stacklevel=3,
            )
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
        if self._token_reset is not None:
            _current_token.reset(self._token_reset)
            self._token_reset = None
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
    token = CancelToken(label, seconds, _current_token.get())
    reset = _current_token.set(token)
    try:
        job = _Job(func, args, kwargs)  # copies the context, token included
    finally:
        _current_token.reset(reset)
    # Real time, like the wait itself: a fake clock advanced by sleeps inside the call
    # (e.g. a retry) must not turn a quick call into a late one.
    start = time.monotonic()
    _workers.submit(job)
    if not job.done.acquire(timeout=seconds):
        _workers.abandon(job)
        token.cancel()
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


# Message of the cancellations a timer sends itself. Python 3.10 has no
# ``Task.cancelling()`` counter, so there the message is how a timer tells its own
# cancellation apart from one sent by someone else (best effort: two cancellations that
# arrive before the task resumes are delivered as one, as in the 3.10 stdlib).
_TIMER_CANCEL = "python_wrap.timeout expired"


def _is_timer_cancel(exc: BaseException | None) -> bool:
    """True if ``exc`` is the CancelledError a timer sent (3.10 path only)."""
    return isinstance(exc, asyncio.CancelledError) and exc.args[:1] == (_TIMER_CANCEL,)


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
                task.cancel(_TIMER_CANCEL)

            self._handle = asyncio.get_running_loop().call_later(self._seconds, fire)
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            self._handle.cancel()
            if self._fired and _is_timer_cancel(exc):
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
        self._cancelling = 0
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
            task.cancel(_TIMER_CANCEL)

        def fire() -> None:
            self._fired = True
            self._cancels += 1
            task.cancel(_TIMER_CANCEL)
            self._handles.append(loop.call_later(self._grace, second))

        self._task = task
        # Like asyncio.timeout: remember the cancel requests already pending on entry, so
        # on exit only the timer's own ones are converted (3.11+).
        cancelling = getattr(task, "cancelling", None)
        self._cancelling = cancelling() if cancelling is not None else 0
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
        if not self._fired or exc_type is not asyncio.CancelledError:
            return
        uncancel = getattr(self._task, "uncancel", None)
        if uncancel is not None:  # 3.11+: withdraw our own requests, then count the rest
            remaining: int = getattr(self._task, "cancelling")()  # noqa: B009 - 3.11+ only
            for _ in range(self._cancels):
                if remaining <= self._cancelling:
                    break
                remaining = uncancel()
            if remaining <= self._cancelling:
                raise TimeoutError from exc
        elif _is_timer_cancel(exc):  # pragma: no cover - exercised by the 3.10 CI job
            raise TimeoutError from exc


async def _await_with_grace(awaitable: Awaitable[T], label: str, seconds: float, grace: float) -> T:
    """Run ``awaitable`` as a child task; on expiry cancel it and wait ``grace`` for cleanup."""
    task = asyncio.ensure_future(awaitable)
    try:
        await asyncio.wait({task}, timeout=seconds)
    except BaseException:
        # Cancelled from outside: cancel the child too, give its cleanup the same grace,
        # then re-raise the caller's cancellation. Never leave its outcome unretrieved.
        task.cancel()
        try:
            await asyncio.wait({task}, timeout=grace)
        finally:
            _consume_when_done(task)
        raise
    if task.done():
        return task.result()
    task.cancel()
    try:
        await asyncio.wait({task}, timeout=grace)
    except BaseException:
        _consume_when_done(task)  # cancelled from outside during the grace period
        raise
    if not task.done():
        _core.get_logger(None).warning(
            "%s: still running %ss after the timeout; abandoning it", label, grace
        )
    _consume_when_done(task)
    raise _expired(label, seconds)


def _consume_when_done(task: asyncio.Future[Any]) -> None:
    """Retrieve ``task``'s outcome now if it is done, otherwise once it is."""
    if task.done():
        _consume_result(task)
    else:
        task.add_done_callback(_consume_result)


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

            async_wrapper.__python_wrap_layer__ = ("timeout", self)  # type: ignore[attr-defined]
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

        wrapper.__python_wrap_layer__ = (  # type: ignore[attr-defined]
            "timeout",
            {"seconds": self.seconds, "use_signals": self.use_signals, "sync": True},
        )
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
      Side effects may therefore continue after `TimeoutExpired`. To make the function
      stop itself, poll `cancel_requested()` or call `checkpoint()` (which raises
      `TimeoutExpired`) in long loops: the call's `CancelToken` is cancelled when the
      limit is hit (with signals too, if the code swallowed the exception).
      Workers are pooled daemon threads: an idle one is reused for the next call (at most
      8 are kept idle, each exits after about 5 s without work), a worker whose call timed
      out is abandoned and never reused, and the caller's ``contextvars`` are copied into
      the worker on every call.

    Signals are process-wide. If a non-default ``SIGALRM`` handler or an armed
    ``ITIMER_REAL`` timer already exists when the signal strategy starts, a
    `SignalTimerConflictWarning` is emitted (both are restored afterwards); `doctor`
    reports it as PW010.

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
