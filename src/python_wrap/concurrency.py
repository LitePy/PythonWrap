"""Concurrency and flow-control decorators.

* :func:`timeout` — bound how long a call may run.
* :func:`rate_limit` — at most *N* calls per period, rejecting or waiting.
* :func:`run_in_thread` — start a call in a new thread and get a ``Future`` back.
* :func:`throttle` — run at most once per interval; calls in between are skipped.
* :func:`debounce` — run once activity has stopped for a while.
* :func:`singleflight` — concurrent identical calls share one execution.

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
import signal
import sys
import threading
import weakref
from collections import deque
from collections.abc import Awaitable, Callable, Coroutine, Hashable
from types import FrameType
from typing import Any, Generic, Protocol, TypeVar, cast, overload

from python_wrap import _core
from python_wrap._core import P, R
from python_wrap.errors import RateLimitExceeded, TimeoutExpired

__all__ = [
    "AsyncDebounced",
    "Debounced",
    "debounce",
    "rate_limit",
    "run_in_thread",
    "singleflight",
    "throttle",
    "timeout",
]

T = TypeVar("T")
R_co = TypeVar("R_co", covariant=True)

# ---------------------------------------------------------------------------------------
# timeout
# ---------------------------------------------------------------------------------------

_HAS_SETITIMER = hasattr(signal, "setitimer") and hasattr(signal, "SIGALRM")


def _signals_usable() -> bool:
    return _HAS_SETITIMER and threading.current_thread() is threading.main_thread()


def _timeout_message(func: Callable[..., Any], seconds: float) -> str:
    return f"{_core.qualname(func)} did not finish within {seconds:g}s"


def _call_with_alarm(func: Callable[P, R], seconds: float, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run ``func`` under a SIGALRM/``setitimer`` deadline, cooperating with outer ones."""
    start = _core.monotonic()
    deadline = start + seconds
    expired = TimeoutExpired(_timeout_message(func, seconds), seconds=seconds)
    outer_fired = False

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_delay, previous_interval = signal.getitimer(signal.ITIMER_REAL)

    def handler(signum: int, frame: FrameType | None) -> None:
        nonlocal outer_fired
        if _core.monotonic() >= deadline - 1e-3 or not previous_delay:
            raise expired
        # The alarm belongs to an enclosing timeout whose deadline comes first.
        outer_fired = True
        signal.signal(signal.SIGALRM, previous_handler)
        if callable(previous_handler):
            previous_handler(signum, frame)

    signal.signal(signal.SIGALRM, handler)
    delay = seconds if not previous_delay else min(seconds, previous_delay)
    signal.setitimer(signal.ITIMER_REAL, delay)
    try:
        return func(*args, **kwargs)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if not outer_fired:
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_delay:
                remaining = previous_delay - (_core.monotonic() - start)
                # Re-arm the enclosing timer; if its deadline already passed, fire ASAP.
                signal.setitimer(signal.ITIMER_REAL, max(remaining, 1e-6), previous_interval)


def _call_in_worker(func: Callable[P, R], seconds: float, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run ``func`` in a daemon thread and stop waiting for it after ``seconds``."""
    future: concurrent.futures.Future[R] = concurrent.futures.Future()
    context = contextvars.copy_context()

    def runner() -> None:
        try:
            future.set_result(context.run(func, *args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)

    worker = threading.Thread(target=runner, name=f"timeout-{_core.qualname(func)}", daemon=True)
    worker.start()
    try:
        return future.result(timeout=seconds)
    except concurrent.futures.TimeoutError:
        raise TimeoutExpired(_timeout_message(func, seconds), seconds=seconds) from None


if sys.version_info >= (3, 11):

    async def _await_with_deadline(
        awaitable: Awaitable[T], func: Callable[..., Any], seconds: float
    ) -> T:
        """Await under :func:`asyncio.timeout`; convert only *our* expiry to TimeoutExpired."""
        cm = asyncio.timeout(seconds)
        try:
            async with cm:
                return await awaitable
        except TimeoutError as exc:
            if not cm.expired():
                raise  # raised by the wrapped function itself: propagate unchanged
            raise TimeoutExpired(_timeout_message(func, seconds), seconds=seconds) from exc

else:  # pragma: no cover - exercised by the Python 3.10 CI job

    async def _await_with_deadline(
        awaitable: Awaitable[T], func: Callable[..., Any], seconds: float
    ) -> T:
        """Run the awaitable as a task; convert only *our* expiry to TimeoutExpired."""
        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait({task}, timeout=seconds)
        except asyncio.CancelledError:
            task.cancel()
            raise
        if done:
            return task.result()  # exceptions from the function propagate unchanged
        task.cancel()
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            exc = task.exception()
            raise TimeoutExpired(_timeout_message(func, seconds), seconds=seconds) from exc
        raise TimeoutExpired(_timeout_message(func, seconds), seconds=seconds)


def timeout(
    seconds: float, *, use_signals: bool | None = None
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Raise :class:`~python_wrap.errors.TimeoutExpired` if a call runs longer than ``seconds``.

    ``TimeoutExpired`` subclasses the built-in ``TimeoutError``.

    * ``async def`` functions are cancelled at the deadline (with :func:`asyncio.timeout`
      on Python 3.11+, or by running the call as a task on 3.10). Only the decorator's own
      deadline is turned into ``TimeoutExpired``: a ``TimeoutError`` raised by the wrapped
      function itself propagates unchanged.
    * Sync functions, with signals (the default on Unix when called from the main thread):
      a ``SIGALRM`` timer from :func:`signal.setitimer` interrupts the call. The previous
      ``SIGALRM`` handler and any already-armed timer are restored afterwards, so nested
      timeouts work.
    * Sync functions, without signals (Windows, non-main threads, or
      ``use_signals=False``): the call runs in a daemon worker thread and the caller stops
      waiting at the deadline. **This fallback cannot stop the work** — the function keeps
      running in the background until it returns on its own, and its result is discarded.

    Warning:
        With the signal strategy, ``TimeoutExpired`` is raised *inside* the wrapped
        function, at whatever line it is executing when the alarm fires. A broad
        ``except Exception:`` (or ``except TimeoutError:``) in that code catches it like
        any other error; if the code then carries on, the time limit is silently lost and
        the call returns normally. ``TimeoutExpired`` deliberately stays an ``Exception``
        (it subclasses ``TimeoutError``), so re-raise it explicitly in such handlers::

            except TimeoutExpired:
                raise
            except Exception:
                ...

        The same applies to context managers that suppress exceptions and to
        ``finally`` blocks that ``return``. ``async def`` functions have the analogous
        caveat for code that swallows ``asyncio.CancelledError``.

    Args:
        seconds: Time limit in seconds; must be positive.
        use_signals: ``True`` forces the signal strategy, ``False`` forces the thread
            fallback, ``None`` (default) picks signals when they are usable.

    Returns:
        A decorator that preserves the wrapped function's signature.

    Raises:
        ValueError: If ``seconds`` is not positive.
        RuntimeError: If ``use_signals=True`` but ``signal.setitimer`` does not exist on
            this platform (at decoration time), or the call is not made from the main
            thread (at call time).
        TimeoutExpired: From the wrapped call, when the time limit is exceeded.

    Example:
        >>> @timeout(5)
        ... def fetch() -> str:
        ...     return "done"
        >>> fetch()
        'done'
    """
    if seconds <= 0:
        raise ValueError(f"timeout seconds must be positive, got {seconds!r}")
    if use_signals and not _HAS_SETITIMER:
        raise RuntimeError("use_signals=True needs signal.setitimer, unavailable here")

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        if _core.is_async(func):
            async_func = cast(Callable[P, Awaitable[Any]], func)

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                return await _await_with_deadline(async_func(*args, **kwargs), func, seconds)

            return cast(Callable[P, R], async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            if use_signals:
                if not _signals_usable():
                    raise RuntimeError(
                        "use_signals=True: signal-based timeouts only work in the main thread"
                    )
                return _call_with_alarm(func, seconds, *args, **kwargs)
            if use_signals is None and _signals_usable():
                return _call_with_alarm(func, seconds, *args, **kwargs)
            return _call_in_worker(func, seconds, *args, **kwargs)

        return wrapper

    return decorate


# ---------------------------------------------------------------------------------------
# rate_limit
# ---------------------------------------------------------------------------------------

_SWEEP_EVERY = 1024


class _SlidingWindow:
    """A thread-safe sliding-window log of call timestamps, one deque per key."""

    def __init__(self, calls: int, period: float) -> None:
        self.calls = calls
        self.period = period
        self._lock = threading.Lock()
        self._buckets: dict[Hashable, deque[float]] = {}
        self._since_sweep = 0

    def acquire(self, key: Hashable) -> float:
        """Record a call and return 0.0, or return how long to wait before retrying."""
        now = _core.monotonic()
        horizon = now - self.period
        with self._lock:
            self._since_sweep += 1
            if self._since_sweep >= _SWEEP_EVERY:
                self._sweep(horizon)
            bucket = self._buckets.setdefault(key, deque())
            while bucket and bucket[0] <= horizon:
                bucket.popleft()
            if len(bucket) < self.calls:
                bucket.append(now)
                return 0.0
            return bucket[0] + self.period - now

    def _sweep(self, horizon: float) -> None:
        self._since_sweep = 0
        stale = [k for k, b in self._buckets.items() if not b or b[-1] <= horizon]
        for k in stale:
            del self._buckets[k]


def rate_limit(
    calls: int,
    period: float,
    *,
    block: bool = False,
    key: Callable[..., Hashable] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Allow at most ``calls`` calls per ``period`` seconds.

    Uses a sliding-window log: a call is allowed when fewer than ``calls`` calls started
    in the last ``period`` seconds. Unlike a fixed window, this never lets a burst of
    ``2 * calls`` through at a window boundary. Rejected (or waiting) calls are not
    counted. The limiter is thread-safe and is shared by every caller of the decorated
    function — including every instance, for methods — unless ``key`` splits it.

    Args:
        calls: Maximum number of calls per window; at least 1.
        period: Window length in seconds; must be positive.
        block: If ``False`` (default) an over-limit call raises ``RateLimitExceeded``.
            If ``True`` it waits until allowed (``async def`` functions wait with
            ``await``, never blocking the event loop).
        key: Optional function called with the call's arguments (including ``self`` for
            methods) that returns a hashable bucket key, e.g. a user id. Each key gets
            its own limit.

    Returns:
        A decorator that preserves the wrapped function's signature.

    Raises:
        ValueError: If ``calls < 1`` or ``period <= 0``.
        RateLimitExceeded: From the wrapped call when over the limit and ``block`` is
            false. Its ``retry_after`` attribute says how many seconds until a slot frees.

    Example:
        >>> @rate_limit(2, 1.0, key=lambda user, *_: user)
        ... def send(user: str, text: str) -> None: ...
    """
    if calls < 1:
        raise ValueError(f"rate_limit calls must be >= 1, got {calls!r}")
    if period <= 0:
        raise ValueError(f"rate_limit period must be positive, got {period!r}")

    def decorate(func: Callable[P, R]) -> Callable[P, R]:
        window = _SlidingWindow(calls, period)
        name = _core.qualname(func)

        def bucket_key(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Hashable:
            return None if key is None else key(*args, **kwargs)

        def rejected(retry_after: float) -> RateLimitExceeded:
            return RateLimitExceeded(
                f"{name} rate limit of {calls} calls per {period:g}s exceeded; "
                f"retry after {retry_after:.3g}s",
                retry_after=retry_after,
            )

        if _core.is_async(func):
            async_func = cast(Callable[P, Awaitable[Any]], func)

            @functools.wraps(func)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                k = bucket_key(args, kwargs)
                while (wait := window.acquire(k)) > 0:
                    if not block:
                        raise rejected(wait)
                    await _core.async_sleep(wait)
                return await async_func(*args, **kwargs)

            return cast(Callable[P, R], async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            k = bucket_key(args, kwargs)
            while (wait := window.acquire(k)) > 0:
                if not block:
                    raise rejected(wait)
                _core.sleep(wait)
            return func(*args, **kwargs)

        return wrapper

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
    """Run each call in a new thread and return a :class:`concurrent.futures.Future`.

    The future resolves to the function's return value, or raises its exception from
    ``future.result()``. For ``async def`` functions the coroutine is run with
    :func:`asyncio.run` inside the new thread (so it gets its own event loop), and the
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

        def should_run() -> tuple[bool, Any]:
            nonlocal last_start
            now = _core.monotonic()
            with lock:
                if last_start is not None and now - last_start < interval:
                    return False, last_result
                last_start = now
                return True, None

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
    """A debounced sync function: calls return ``None``; the real call happens later."""

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> None: ...

    def cancel(self) -> None:
        """Drop the pending call, if any."""

    def flush(self) -> R_co | None:
        """Run the pending call now and return its result (``None`` if nothing pending)."""


class AsyncDebounced(Protocol[P, R_co]):
    """A debounced ``async def`` function; see :func:`debounce` for the await semantics."""

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Coroutine[Any, Any, R_co]: ...

    def cancel(self) -> None:
        """Drop the pending call; callers awaiting it get ``CancelledError``."""

    def flush(self) -> Coroutine[Any, Any, R_co | None]:
        """Run the pending call now; awaiting returns its result (``None`` if none)."""


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

    async def run(burst: _AsyncBurst) -> Any:
        loop = asyncio.get_running_loop()
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
        await _core.async_sleep(wait)
        burst.timer = None  # past the cancellation point: run even if called again now
        with contextlib.suppress(Exception):  # delivered to callers via burst.outcome
            await run(burst)

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        burst = bursts.get(loop)
        if burst is None:
            burst = bursts[loop] = _AsyncBurst(loop)
        elif burst.timer is not None:
            burst.timer.cancel()
        burst.call = (args, kwargs)
        burst.timer = loop.create_task(sleep_then_run(burst))
        return await asyncio.shield(burst.outcome)

    def cancel() -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loops = list(bursts)
        else:
            loops = [loop]
        for lp in loops:
            burst = bursts.pop(lp, None)
            if burst is not None:
                if burst.timer is not None:
                    burst.timer.cancel()
                burst.outcome.cancel()

    async def flush() -> Any:
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

    * Sync functions: the countdown is a daemon :class:`threading.Timer`, so the function
      runs in the timer's thread. The wrapper returns ``None`` immediately.
    * ``async def`` functions: a call schedules a task on the running loop (cancelling the
      previously scheduled one) and then waits for the burst to finish. **Every caller in
      a burst awaits the same outcome** — the result (or exception) of the final call —
      so superseded callers are not left hanging or cancelled. Calls must be awaited (or
      wrapped in a task) to take effect; a single task awaiting sequentially simply waits
      ``wait`` seconds per call. Bursts are tracked per event loop.

    The wrapper also has:

    * ``.cancel()`` — drop the pending call (async awaiters get ``CancelledError``).
    * ``.flush()`` — run the pending call right now and return its result, or ``None``
      if nothing is pending. For ``async def`` functions it is a coroutine to ``await``.

    State belongs to the decorated function, so on a method it is shared by all
    instances: calls on different instances debounce each other.

    Args:
        wait: Quiet period in seconds; must be non-negative.

    Returns:
        A decorator producing a :class:`Debounced` or :class:`AsyncDebounced` wrapper.

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


def _default_key(*args: Any, **kwargs: Any) -> Hashable:
    return (args, tuple(sorted(kwargs.items()))) if kwargs else args


class _Flights(Generic[T]):
    """Thread-side bookkeeping for singleflight: key -> (in-flight future, leader thread)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.inflight: dict[Hashable, tuple[concurrent.futures.Future[T], int]] = {}


def _consume_outcome(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def _await_shared(loop: asyncio.AbstractEventLoop, task: asyncio.Task[T]) -> T:
    """Wait for a shared task without letting the caller's cancellation cancel it.

    Like :func:`asyncio.shield`, but a cancelled waiter simply detaches: the task's
    eventual exception is not reported as an unhandled "exception in shielded future".
    """
    waiter: asyncio.Future[T] = loop.create_future()

    def relay(done: asyncio.Task[T]) -> None:
        if waiter.done():
            return
        if done.cancelled():
            waiter.cancel()
        elif (exc := done.exception()) is not None:
            waiter.set_exception(exc)
        else:
            waiter.set_result(done.result())

    task.add_done_callback(relay)
    try:
        return await waiter
    except asyncio.CancelledError:
        task.remove_done_callback(relay)
        if waiter.done() and not waiter.cancelled():
            waiter.exception()  # outcome arrived just as we were cancelled
        raise


@overload
def singleflight(func: Callable[P, R], /) -> Callable[P, R]: ...
@overload
def singleflight(
    func: None = None, /, *, key: Callable[..., Hashable] | None = None
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...
def singleflight(
    func: Callable[P, R] | None = None,
    /,
    *,
    key: Callable[..., Hashable] | None = None,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
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
            tasks: weakref.WeakKeyDictionary[
                asyncio.AbstractEventLoop, dict[Hashable, asyncio.Task[Any]]
            ] = weakref.WeakKeyDictionary()

            @functools.wraps(f)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                k = key_func(*args, **kwargs)
                loop = asyncio.get_running_loop()
                inflight = tasks.setdefault(loop, {})
                task = inflight.get(k)
                if task is not None and task is asyncio.current_task():
                    # Re-entrant call from inside the shared task: waiting for ourselves
                    # would deadlock, so run the function directly.
                    return await async_f(*args, **kwargs)
                if task is None:

                    async def run() -> Any:
                        try:
                            return await async_f(*args, **kwargs)
                        finally:
                            if inflight.get(k) is task:
                                del inflight[k]

                    task = inflight[k] = loop.create_task(run())
                    # Retrieve the outcome even if every caller was cancelled meanwhile,
                    # so asyncio never logs "exception was never retrieved".
                    task.add_done_callback(_consume_outcome)
                return await _await_shared(loop, task)

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
