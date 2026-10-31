"""Slow-call detection for synchronous and asynchronous functions."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any, TypeVar, cast

from python_wrap import _core
from python_wrap._runtime import seconds
from python_wrap.logs import _ArgBinder, _signature
from python_wrap.redaction import DEFAULT_REDACT, is_sensitive, redact

__all__ = ["timed"]
F = TypeVar("F", bound=Callable[..., Any])


def timed(
    *,
    warn_over: float | str = "2s",
    log_args: bool = False,
    redact_names: tuple[str, ...] = DEFAULT_REDACT,
    logger: Any = None,
    level: int = logging.WARNING,
) -> Callable[[F], F]:
    """Log only calls at or above the threshold, masking argument names and nested keys.

    Error/cancellation paths are timed too; instrumentation never replaces the outcome.
    The logging handler's formatter controls final output size; no return value is logged.
    """
    threshold = seconds(warn_over, positive=False)

    def decorate(func: F) -> F:
        binder = _ArgBinder(_signature(func), lambda name: is_sensitive(name, redact_names))
        name = _core.qualname(func)
        log = _core.get_logger(logger)

        def finish(start: float, args: tuple[Any, ...], kw: dict[str, Any]) -> None:
            elapsed = _core.perf_counter() - start
            if elapsed < threshold:
                return
            try:
                payload = redact(binder(args, kw), fragments=redact_names) if log_args else {}
                log.log(level, "%s slow call: %.6fs arguments=%r", name, elapsed, payload)
                _core.emit("timing.slow", name, seconds=elapsed)
            except Exception:
                pass

        if _core.is_async(func):

            @functools.wraps(func)
            async def aw(*args: Any, **kw: Any) -> Any:
                start = _core.perf_counter()
                try:
                    return await func(*args, **kw)
                finally:
                    finish(start, args, kw)

            return cast(F, aw)

        @functools.wraps(func)
        def sw(*args: Any, **kw: Any) -> Any:
            start = _core.perf_counter()
            try:
                return func(*args, **kw)
            finally:
                finish(start, args, kw)

        return cast(F, sw)

    return decorate
