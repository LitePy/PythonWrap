"""Shared plumbing for every decorator in the package.

Rules every module follows:

* Never configure logging. Log through `get_logger`; the package logger carries a
  ``NullHandler`` so nothing is printed unless the application configures logging.
* Support ``async def`` functions: branch on `is_async` and return an ``async def``
  wrapper, so ``inspect.iscoroutinefunction(wrapped)`` stays true.
* Read time through ``_core.monotonic()`` / ``_core.perf_counter()`` and wait through
  ``_core.sleep()`` / ``_core.async_sleep()`` — always as module attributes, never
  imported by name — so tests can patch one place to get a fake clock.
* Honour the process-wide test overrides (see below).

Test overrides contract
-----------------------
``_core.overrides`` is a single process-wide `Overrides` instance, mutated in
place (never replaced), so it may be read as ``_core.overrides.<field>`` at any time.
Reads are plain attribute reads (thread-safe under the GIL); changing it is a test-time
operation done through `set_overrides` (which holds ``overrides_lock``) or the
public helpers in `python_wrap.testing`. Modules must read the field **at call
time**, not at decoration time, so a test can flip it after import:

* ``no_sleep``: `sleep` / `async_sleep` return immediately (the async one
  still yields to the event loop once). Handled here; other modules need do nothing as
  long as they wait only through ``_core``. A fake clock installed by ``fake_clock()``
  replaces the sleeps and so still advances time.
* ``max_attempts``: when not ``None``, ``retry`` uses
  ``min(configured_max_attempts, _core.overrides.max_attempts)`` attempts.
* ``disable_rate_limits``: when true, ``rate_limit`` / ``throttle`` (and any other
  limiter that delays or rejects calls) let every call through immediately.
* ``disable_caching``: when true, ``memoize`` / ``cache`` / ``once``-style caches
  neither read nor write their stores and always call the wrapped function.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, fields
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")
F = TypeVar("F", bound=Callable[..., Any])

LOGGER_NAME = "python_wrap"

LoggerLike = logging.Logger | str | None

# Time sources, patched by tests. Call them as ``_core.monotonic()``.
monotonic: Callable[[], float] = time.monotonic
perf_counter: Callable[[], float] = time.perf_counter


@dataclass
class Overrides:
    """Process-wide switches for tests. See the module docstring for the contract."""

    no_sleep: bool = False
    max_attempts: int | None = None
    disable_rate_limits: bool = False
    disable_caching: bool = False


overrides = Overrides()
overrides_lock = threading.RLock()


def set_overrides(**values: Any) -> dict[str, Any]:
    """Set override fields atomically and return their previous values.

    Raises:
        TypeError: If a name is not a field of `Overrides`.
        ValueError: If ``max_attempts`` is not ``None`` or a positive integer.
    """
    names = {f.name for f in fields(Overrides)}
    unknown = set(values) - names
    if unknown:
        raise TypeError(f"unknown override(s): {', '.join(sorted(unknown))}")
    attempts = values.get("max_attempts")
    if attempts is not None and (not isinstance(attempts, int) or attempts < 1):
        raise ValueError("max_attempts override must be None or an integer >= 1")
    with overrides_lock:
        previous = {name: getattr(overrides, name) for name in values}
        for name, value in values.items():
            setattr(overrides, name, value)
    return previous


def sleep(seconds: float) -> None:
    """Blocking sleep; a no-op while ``overrides.no_sleep`` is set."""
    if overrides.no_sleep:
        return
    time.sleep(seconds)


async def async_sleep(seconds: float) -> None:
    """Indirection over `asyncio.sleep` so tests can patch it.

    While ``overrides.no_sleep`` is set it only yields to the event loop once.
    """
    await asyncio.sleep(0 if overrides.no_sleep else seconds)


_default_logger: LoggerLike = None


def get_logger(logger: LoggerLike = None) -> logging.Logger:
    """Resolve a ``logger=`` argument: a Logger, a logger name, or the package logger."""
    if logger is None:
        logger = _default_logger  # set by python_wrap.config.configure(logger=...)
    if isinstance(logger, logging.Logger):
        return logger
    return logging.getLogger(logger or LOGGER_NAME)


def is_async(func: Callable[..., Any]) -> bool:
    """True if calling ``func`` returns a coroutine.

    Covers ``async def`` functions and methods, ``functools.partial`` objects around
    them, and callable instances whose class defines ``async def __call__``. A class is
    never async: calling it constructs an instance, even if the class has an async
    ``__call__``.
    """
    while isinstance(func, functools.partial):
        func = func.func
    if inspect.iscoroutinefunction(func):
        return True
    if inspect.isclass(func) or inspect.isfunction(func) or inspect.ismethod(func):
        return False
    # Look the method up on the type, as the call itself does.
    call = inspect.getattr_static(type(func), "__call__", None)
    return call is not None and inspect.iscoroutinefunction(call)


def qualname(func: Callable[..., Any]) -> str:
    """A readable name for log messages."""
    return getattr(func, "__qualname__", None) or getattr(func, "__name__", None) or repr(func)


# ---------------------------------------------------------------------------- events
# One process-wide hook for metrics. Decorators call ``_core.emit(...)`` at interesting
# moments (a retry, a cache hit, a circuit opening); ``python_wrap.events`` is the public
# face. With no listener registered, ``emit`` is a single list check.


@dataclass(frozen=True)
class Event:
    """Something a decorator did, for metrics and tracing.

    Attributes:
        name: Dotted event name, e.g. ``"retry.attempt_failed"``.
        function: Qualified name of the decorated function (or the breaker/limiter name).
        fields: Event-specific data, e.g. ``{"attempt": 2, "next_delay": 0.5}``.
    """

    name: str
    function: str
    fields: dict[str, Any]


listeners: list[Callable[[Event], None]] = []
listeners_lock = threading.Lock()


def emit(name: str, function: str, **fields: Any) -> None:
    """Deliver an event to every listener. A failing listener is logged, never raised."""
    if not listeners:
        return
    event = Event(name, function, fields)
    for listener in tuple(listeners):
        try:
            listener(event)
        except Exception:
            get_logger().exception("python_wrap event listener %r failed", listener)
