"""Run a function every N seconds in the background.

`periodic` is a tiny background loop, not a scheduler: no cron expressions, no
persistence, no catch-up. The decorated function stays directly callable and gains
``start()``, ``stop()`` and a few read-only attributes.

Timing rules:

* The schedule is **real time**. ``fast_mode()`` and ``fake_clock()`` never shorten
  it (they only affect backoff waits, see `python_wrap.testing`); a periodic loop that
  ran "instantly" would spin. Test with tiny intervals, or call the function directly.
* Intervals are measured **start to start** and do not drift: the n-th tick is due at
  ``first + n * interval``. If a run is still going when the next tick is due, the missed
  ticks are **skipped** (never run in a burst) and the loop resumes at the next tick.
* ``jitter`` moves each run by a uniformly random offset of up to ``± jitter * interval``
  around its tick. Jitter never accumulates, because ticks are computed without it.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import inspect
import logging
import math
import random
import threading
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from typing import Any, Generic, Literal, ParamSpec, Protocol, TypeVar, overload

from python_wrap import _core
from python_wrap._core import LoggerLike

__all__ = ["AsyncPeriodic", "Periodic", "periodic"]

P = ParamSpec("P")
R = TypeVar("R")

OnError = Literal["log", "stop", "raise"]
_ON_ERROR = ("log", "stop", "raise")

# Patched by tests; the schedule is real time on purpose (see the module docstring).
_clock: Callable[[], float] = time.monotonic
_uniform: Callable[[float, float], float] = random.uniform


def _next_tick(due: float, now: float, interval: float) -> float:
    """The tick after ``due``, skipping every tick that is already in the past."""
    nxt = due + interval
    if nxt <= now:
        nxt += (math.floor((now - nxt) / interval) + 1) * interval
    return nxt


def _delay_until(due: float, now: float, interval: float, jitter: float) -> float:
    """Seconds to wait until ``due``, moved by the jitter and never negative."""
    offset = _uniform(-jitter, jitter) * interval if jitter else 0.0
    return max(0.0, due + offset - now)


class _Base(Generic[P, R]):
    """State and policy shared by the sync and async variants."""

    def __init__(
        self,
        func: Callable[..., Any],
        interval: float,
        jitter: float,
        initial_delay: float,
        max_runs: int | None,
        on_error: OnError,
        name: str | None,
        logger: LoggerLike,
        level: int,
    ) -> None:
        self._func = func
        self._interval = interval
        self._jitter = jitter
        self._initial_delay = initial_delay
        self._max_runs = max_runs
        self._on_error = on_error
        self._name = name or _core.qualname(func)
        self._logger = logger
        self._level = level
        self._runs = 0
        self._last_error: BaseException | None = None
        self._pending: BaseException | None = None  # on_error="raise", surfaced by stop()
        self._lock = threading.Lock()
        functools.update_wrapper(self, func)  # type: ignore[arg-type]

    def __reduce__(self) -> str:
        # Pickle by reference, like a function: the object at ``module.qualname`` is
        # this one. The background loop and its counters stay in this process.
        return str(self.__dict__["__qualname__"])  # set by update_wrapper

    @property
    def runs(self) -> int:
        """How many times the function has been run since `start` (failed runs count)."""
        return self._runs

    @property
    def last_error(self) -> BaseException | None:
        """The exception of the most recent failed run, or ``None``."""
        return self._last_error

    def _ran(self, seconds: float) -> None:
        _core.emit("periodic.run", self._name, run=self._runs, seconds=seconds)

    def _failed(self, exc: Exception) -> bool:
        """Record a failed run. Returns True if the loop should continue."""
        self._last_error = exc
        _core.emit("periodic.failed", self._name, run=self._runs, exception=exc)
        if self._on_error == "log":
            _core.get_logger(self._logger).log(
                self._level, "periodic %s failed (run %d)", self._name, self._runs, exc_info=exc
            )
            return True
        if self._on_error == "stop":
            _core.get_logger(self._logger).log(
                self._level,
                "periodic %s failed (run %d); stopping",
                self._name,
                self._runs,
                exc_info=exc,
            )
        else:
            self._pending = exc
        return False

    def _done(self) -> bool:
        return self._max_runs is not None and self._runs >= self._max_runs

    def _surface(self) -> None:
        exc, self._pending = self._pending, None
        if exc is not None:
            raise exc

    def _reset(self) -> None:
        self._runs = 0
        self._last_error = None
        self._pending = None


class Periodic(_Base[P, R]):
    """A function that runs in a background daemon thread; see `periodic`.

    Calling the object calls the wrapped function directly. Attributes: `runs`,
    `last_error`, `running`.
    """

    def __init__(self, func: Callable[P, R], *config: Any) -> None:
        super().__init__(func, *config)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        return self._func(*args, **kwargs)  # type: ignore[no-any-return]

    @property
    def running(self) -> bool:
        """True while the background thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, *args: P.args, **kwargs: P.kwargs) -> Periodic[P, R]:
        """Start the background thread; ``args`` and ``kwargs`` go to every run.

        Returns:
            This object, so ``f.start()`` can be chained.

        Raises:
            RuntimeError: If it is already running.
        """
        with self._lock:
            if self.running:
                raise RuntimeError(f"periodic {self._name} is already running")
            self._reset()
            self._stop_event = threading.Event()
            ctx = contextvars.copy_context()
            thread = threading.Thread(
                target=ctx.run,
                args=(self._loop, self._stop_event, args, kwargs),
                name=f"python-wrap-periodic:{self._name}",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        return self

    def stop(self, timeout: float | None = 5.0) -> bool:
        """Stop the loop and wait up to ``timeout`` seconds for the thread to end.

        A run that is in progress is allowed to finish (a thread cannot be
        interrupted). Calling it again, or on a loop that never started, is harmless.
        Called from inside the function itself it only requests the stop.

        Args:
            timeout: Seconds to wait for the thread; ``None`` waits forever.

        Returns:
            True if the thread has ended, False if it is still finishing a run.

        Raises:
            Exception: With ``on_error="raise"``, the exception that ended the loop
                (raised once).
        """
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                _core.get_logger(self._logger).warning(
                    "periodic %s did not stop within %s seconds", self._name, timeout
                )
                return False
        self._surface()
        return True

    @contextlib.contextmanager
    def running_in_background(self, *args: P.args, **kwargs: P.kwargs) -> Iterator[Periodic[P, R]]:
        """Start the loop for the duration of a ``with`` block, then `stop` it."""
        self.start(*args, **kwargs)
        try:
            yield self
        finally:
            self.stop()

    def _loop(
        self,
        stop: threading.Event,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        due = _clock() + self._initial_delay
        if stop.wait(self._initial_delay):
            return
        while True:
            began = _clock()
            self._runs += 1
            try:
                self._func(*args, **kwargs)
            except Exception as exc:
                if not self._failed(exc):
                    return
            else:
                self._ran(_clock() - began)
            if self._done():
                return
            now = _clock()
            due = _next_tick(due, now, self._interval)
            if stop.wait(_delay_until(due, now, self._interval, self._jitter)):
                return


class AsyncPeriodic(_Base[P, R]):
    """An ``async def`` function that runs as a task on the current loop; see `periodic`.

    Calling the object calls the coroutine function directly. Attributes: `runs`,
    `last_error`, `running`.
    """

    def __init__(self, func: Callable[P, Coroutine[Any, Any, R]], *config: Any) -> None:
        super().__init__(func, *config)
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        mark = getattr(inspect, "markcoroutinefunction", None)  # Python 3.12+
        if mark is not None:
            mark(self)

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Coroutine[Any, Any, R]:
        return self._func(*args, **kwargs)  # type: ignore[no-any-return]

    @property
    def running(self) -> bool:
        """True while the background task has not finished."""
        task = self._task
        return task is not None and not task.done()

    def start(self, *args: P.args, **kwargs: P.kwargs) -> AsyncPeriodic[P, R]:
        """Start the background task on the running loop; arguments go to every run.

        Returns:
            This object, so ``f.start()`` can be chained.

        Raises:
            RuntimeError: If called outside a running event loop, or if already running.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            raise RuntimeError(
                f"periodic {self._name}: start() must be called from a running event loop "
                "(inside an async function). Use a plain def function for a thread-based loop."
            ) from None
        if self.running:
            raise RuntimeError(f"periodic {self._name} is already running")
        self._reset()
        self._stopping = False
        self._task = loop.create_task(self._loop(args, kwargs), name=f"periodic:{self._name}")
        return self

    async def stop(self) -> bool:
        """Cancel the task (including a run in progress) and wait for it to end.

        Calling it again, or on a loop that never started, is harmless. Called from
        inside the function itself it ends the loop after that run returns.

        Returns:
            True once the task has ended.

        Raises:
            Exception: With ``on_error="raise"``, the exception that ended the loop
                (raised once).
        """
        task = self._task
        if task is not None and not task.done():
            if task is asyncio.current_task():
                self._stopping = True
                return True
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._surface()
        return True

    @contextlib.asynccontextmanager
    async def running_in_background(
        self, *args: P.args, **kwargs: P.kwargs
    ) -> AsyncIterator[AsyncPeriodic[P, R]]:
        """Start the loop for the duration of an ``async with`` block, then `stop` it."""
        self.start(*args, **kwargs)
        try:
            yield self
        finally:
            await self.stop()

    async def _loop(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        due = _clock() + self._initial_delay
        await asyncio.sleep(self._initial_delay)
        while not self._stopping:
            began = _clock()
            self._runs += 1
            try:
                await self._func(*args, **kwargs)
            except Exception as exc:
                if not self._failed(exc):
                    return
            else:
                self._ran(_clock() - began)
            if self._done():
                return
            now = _clock()
            due = _next_tick(due, now, self._interval)
            await asyncio.sleep(_delay_until(due, now, self._interval, self._jitter))


class _Decorator(Protocol):
    @overload
    def __call__(  # type: ignore[overload-overlap]
        self, func: Callable[P, Coroutine[Any, Any, R]], /
    ) -> AsyncPeriodic[P, R]: ...
    @overload
    def __call__(self, func: Callable[P, R], /) -> Periodic[P, R]: ...


def periodic(
    interval: float,
    *,
    jitter: float = 0.0,
    initial_delay: float = 0.0,
    max_runs: int | None = None,
    on_error: OnError = "log",
    name: str | None = None,
    logger: LoggerLike = None,
    level: int = logging.ERROR,
) -> _Decorator:
    """Run the decorated function every ``interval`` seconds in the background.

    The function stays directly callable. The decorated object adds ``start()``,
    ``stop()``, ``running``, ``runs``, ``last_error`` and ``running_in_background()``
    (a context manager that starts and stops cleanly). A plain function runs in a daemon
    thread; an ``async def`` function runs as a task on the current event loop
    (``start()`` must be called inside it, and ``await f.stop()`` ends it). ``start()``
    arguments are passed to every run, and the caller's ``contextvars`` are copied.

    Runs start every ``interval`` seconds measured start to start, without drift. A run
    that outlasts the interval makes the loop skip the missed ticks instead of running
    in a burst. The schedule is real time: `fast_mode` and `fake_clock` do not shorten it.

    To use it on a method, decorate the bound method: ``periodic(5)(self.refresh)``.
    Decorating a function that takes ``self`` or ``cls`` first is rejected.

    Args:
        interval: Seconds between run starts. Must be greater than zero.
        jitter: Fraction of ``interval`` (``0`` to less than ``1``) by which each run is
            randomly moved earlier or later, e.g. ``0.1`` is +/-10 percent.
        initial_delay: Seconds before the first run (no jitter). Default: run at once.
        max_runs: Stop by itself after this many runs (failed runs count). Default: forever.
        on_error: What an exception in a run does. ``"log"`` logs it and carries on;
            ``"stop"`` logs it and ends the loop; ``"raise"`` ends the loop and re-raises
            the exception, once, from `stop()` (so it never vanishes in a thread). In every
            mode it is kept in ``last_error`` and published as a ``periodic.failed`` event.
        name: Name for logs, events and the thread. Default: the function's qualified name.
        logger: Logger, logger name or ``None`` for the package logger.
        level: Level for failure logs. Default: ``logging.ERROR``.

    Returns:
        A decorator producing a `Periodic` (plain function) or `AsyncPeriodic`
        (``async def``).

    Raises:
        ValueError: For an invalid ``interval``, ``jitter``, ``initial_delay``,
            ``max_runs`` or ``on_error``.
        TypeError: When decorating something that is not callable, or an unbound method.

    Events:
        ``periodic.run`` (fields ``run``, ``seconds``) and ``periodic.failed`` (fields
        ``run``, ``exception``).

    Example:
        ```python
        from python_wrap.periodic import periodic

        @periodic(0.01, max_runs=3)
        def tick() -> None:
            ...

        with tick.running_in_background():
            pass
        ```
    """
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or not interval > 0:
        raise ValueError("interval must be a number greater than 0")
    if not isinstance(jitter, (int, float)) or not 0 <= jitter < 1:
        raise ValueError("jitter must be at least 0 and less than 1")
    if not isinstance(initial_delay, (int, float)) or not initial_delay >= 0:
        raise ValueError("initial_delay must be a number >= 0")
    if max_runs is not None and (not isinstance(max_runs, int) or max_runs < 1):
        raise ValueError("max_runs must be None or an integer >= 1")
    if on_error not in _ON_ERROR:
        raise ValueError(f"on_error must be one of {', '.join(map(repr, _ON_ERROR))}")

    def decorate(func: Callable[..., Any]) -> Any:
        if not callable(func):
            raise TypeError("periodic() decorates a function")
        _reject_unbound_method(func)
        config = (interval, jitter, initial_delay, max_runs, on_error, name, logger, level)
        if _core.is_async(func):
            return AsyncPeriodic(func, *config)
        return Periodic(func, *config)

    return decorate


def _reject_unbound_method(func: Callable[..., Any]) -> None:
    if not inspect.isfunction(func):
        return
    params = list(inspect.signature(func).parameters)
    if params and params[0] in ("self", "cls"):
        raise TypeError(
            f"periodic cannot decorate the method {func.__qualname__} in a class body: "
            "decorate the bound method instead, e.g. periodic(5)(self.refresh)"
        )
