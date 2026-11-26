"""`singleflight`: concurrent identical calls share one execution."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import sys
import threading
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Hashable
from typing import (
    Any,
    Final,
    Generic,
    ParamSpec,
    TypeVar,
    cast,
    overload,
)

from python_wrap import _core, _fork
from python_wrap._core import F, P, R

T = TypeVar("T")
R_co = TypeVar("R_co", covariant=True)
P2 = ParamSpec("P2")
S = TypeVar("S")


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
        self.lock = _fork.ForkSafeLock()
        self.inflight: dict[Hashable, tuple[concurrent.futures.Future[T], int]] = {}
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self.inflight.clear()


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
            loops_lock = _fork.ForkSafeLock()  # loops in several threads share the mapping

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

            async_wrapper.__dict__["_fork_reset"] = _fork.register_reset(flights_by_loop.clear)
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
