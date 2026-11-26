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
import contextvars
import functools
import threading
from collections.abc import Callable, Coroutine
from typing import (
    Any,
    ParamSpec,
    Protocol,
    TypeVar,
    overload,
)

from python_wrap import _core
from python_wrap._core import P

__all__ = [
    "AsyncDebounced",
    "CancelToken",
    "Debounced",
    "Limit",
    "RateLimiter",
    "SignalTimerConflictWarning",
    "Timeout",
    "cancel_requested",
    "checkpoint",
    "current_token",
    "debounce",
    "limiter_of",
    "rate_limit",
    "run_in_thread",
    "singleflight",
    "throttle",
    "timeout",
]

from python_wrap._buckets import (  # noqa: F401 - re-exported for compatibility
    _SWEEP_BUCKETS_EVERY,
    _Buckets,
)
from python_wrap.debounce import (  # noqa: F401 - re-exported for compatibility
    AsyncDebounced,
    Debounced,
    _AsyncBurst,
    _AsyncDebouncedOnClass,
    _AsyncDebState,
    _debounce_async,
    _debounce_sync,
    _DebounceDecorator,
    _DebouncedOnClass,
    _Pending,
    _SyncDebState,
    _ThrottleDecorator,
    _ThrottleState,
    debounce,
    throttle,
)
from python_wrap.ratelimit import (  # noqa: F401 - re-exported for compatibility
    _SWEEP_EVERY,
    Limit,
    RateLimiter,
    _describe,
    _limits,
    _SlidingWindow,
    limiter_of,
    rate_limit,
)
from python_wrap.singleflight import (  # noqa: F401 - re-exported for compatibility
    _EAGER_TASKS,
    _KWARGS,
    _AsyncFlight,
    _await_flight,
    _default_key,
    _Flights,
    _KwargsMark,
    _start_task,
    singleflight,
)
from python_wrap.timeouts import (  # noqa: F401 - re-exported for compatibility
    _BLOCK,
    _HAS_SETITIMER,
    _MAX_IDLE_WORKERS,
    _TIMER_CANCEL,
    _WORKER_IDLE_SECONDS,
    CancelToken,
    SignalTimerConflictWarning,
    Timeout,
    _Alarm,
    _await_with_deadline,
    _await_with_grace,
    _call_in_worker,
    _consume_result,
    _consume_when_done,
    _current_token,
    _expired,
    _foreign_alarm,
    _GraceTimer,
    _is_timer_cancel,
    _Job,
    _signals_usable,
    _timeout_message,
    _WorkerPool,
    _workers,
    cancel_requested,
    checkpoint,
    current_token,
    foreign_alarm_state,
    timeout,
)

T = TypeVar("T")
R_co = TypeVar("R_co", covariant=True)
P2 = ParamSpec("P2")
S = TypeVar("S")


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
