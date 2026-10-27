"""Shared plumbing for every decorator in the package.

Rules every module follows:

* Never configure logging. Log through :func:`get_logger`; the package logger carries a
  ``NullHandler`` so nothing is printed unless the application configures logging.
* Support ``async def`` functions: branch on :func:`is_async` and return an ``async def``
  wrapper, so ``inspect.iscoroutinefunction(wrapped)`` stays true.
* Read time through ``_core.monotonic()`` / ``_core.perf_counter()`` and wait through
  ``_core.sleep()`` / ``_core.async_sleep()`` — always as module attributes, never
  imported by name — so tests can patch one place to get a fake clock.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")
F = TypeVar("F", bound=Callable[..., Any])

LOGGER_NAME = "python_wrap"

LoggerLike = logging.Logger | str | None

# Time sources, patched by tests. Call them as ``_core.monotonic()``.
monotonic: Callable[[], float] = time.monotonic
perf_counter: Callable[[], float] = time.perf_counter
sleep: Callable[[float], None] = time.sleep


async def async_sleep(seconds: float) -> None:
    """Indirection over :func:`asyncio.sleep` so tests can patch it."""
    await asyncio.sleep(seconds)


def get_logger(logger: LoggerLike = None) -> logging.Logger:
    """Resolve a ``logger=`` argument: a Logger, a logger name, or the package logger."""
    if isinstance(logger, logging.Logger):
        return logger
    return logging.getLogger(logger or LOGGER_NAME)


def is_async(func: Callable[..., Any]) -> bool:
    """True for ``async def`` functions, including ones wrapped in ``functools.partial``."""
    while isinstance(func, functools.partial):
        func = func.func
    return inspect.iscoroutinefunction(func)


def qualname(func: Callable[..., Any]) -> str:
    """A readable name for log messages."""
    return getattr(func, "__qualname__", None) or getattr(func, "__name__", None) or repr(func)
