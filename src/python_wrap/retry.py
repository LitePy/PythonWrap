"""Retry, fallback and suppress decorators."""

from __future__ import annotations

import functools
import random
import sys
import warnings
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast, overload

from python_wrap import _core
from python_wrap._core import LoggerLike, P, R

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)

ExceptionTypes = type[BaseException] | tuple[type[BaseException], ...]

__all__ = ["RetryState", "fallback", "retry", "retry_on_exception", "suppress"]

_MISSING: Any = object()


@dataclass(frozen=True)
class RetryState:
    """Snapshot passed to the ``on_retry`` callback before each wait.

    Attributes:
        attempt: Number of the attempt that just failed (1-based).
        exception: The exception that attempt raised.
        next_delay: Seconds about to be slept before the next attempt (jitter included).
        elapsed: Seconds since the first attempt started.
    """

    attempt: int
    exception: BaseException
    next_delay: float
    elapsed: float


def _validate(
    max_attempts: int | None, delay: float, backoff: float, max_delay: float | None, jitter: float
) -> None:
    if max_attempts is not None and max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1 or None, got {max_attempts!r}")
    if delay < 0:
        raise ValueError(f"delay must be >= 0, got {delay!r}")
    if backoff < 1:
        raise ValueError(f"backoff must be >= 1, got {backoff!r}")
    if jitter < 0:
        raise ValueError(f"jitter must be >= 0, got {jitter!r}")
    if max_delay is not None and max_delay < 0:
        raise ValueError(f"max_delay must be >= 0, got {max_delay!r}")


def _compute_delay(
    attempt: int, delay: float, backoff: float, max_delay: float | None, jitter: float
) -> float:
    try:
        wait = delay * backoff ** (attempt - 1)
    except OverflowError:
        wait = sys.float_info.max
    if max_delay is not None:
        wait = min(wait, max_delay)
    if jitter:
        wait += random.uniform(0, jitter)
    return wait


@overload
def retry(
    func: Callable[P, R],
    /,
    *,
    max_attempts: int | None = ...,
    delay: float = ...,
    backoff: float = ...,
    max_delay: float | None = ...,
    jitter: float = ...,
    on: ExceptionTypes = ...,
    retry_if: Callable[[BaseException], bool] | None = ...,
    on_retry: Callable[[RetryState], None] | None = ...,
    logger: LoggerLike = ...,
) -> Callable[P, R]: ...
@overload
def retry(
    func: None = None,
    /,
    *,
    max_attempts: int | None = ...,
    delay: float = ...,
    backoff: float = ...,
    max_delay: float | None = ...,
    jitter: float = ...,
    on: ExceptionTypes = ...,
    retry_if: Callable[[BaseException], bool] | None = ...,
    on_retry: Callable[[RetryState], None] | None = ...,
    logger: LoggerLike = ...,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...
def retry(
    func: Callable[P, R] | None = None,
    /,
    *,
    max_attempts: int | None = 3,
    delay: float = 1.0,
    backoff: float = 1.0,
    max_delay: float | None = None,
    jitter: float = 0.0,
    on: ExceptionTypes = Exception,
    retry_if: Callable[[BaseException], bool] | None = None,
    on_retry: Callable[[RetryState], None] | None = None,
    logger: LoggerLike = None,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Retry a function when it raises, optionally with exponential backoff.

    Works bare (``@retry``), with options (``@retry(max_attempts=5)``) and as a plain
    call (``retry(func, max_attempts=5)``). Async functions are awaited and slept
    with ``asyncio`` so the event loop is never blocked.

    The wait before attempt ``n + 1`` is
    ``min(delay * backoff ** (n - 1), max_delay) + random.uniform(0, jitter)``.
    The last exception is re-raised unchanged, with its original traceback, once
    attempts are exhausted.

    Args:
        func: The function to wrap (when used without parentheses).
        max_attempts: Total number of calls allowed, or ``None`` for no limit.
        delay: Base wait in seconds before the second attempt.
        backoff: Multiplier applied to the wait after each failure (>= 1).
        max_delay: Upper bound for the backoff wait (before jitter is added).
        jitter: Up to this many random extra seconds are added to each wait.
        on: Exception type(s) that trigger a retry; anything else propagates at once.
        retry_if: Extra predicate; the exception is retried only if it returns True.
        on_retry: Called with a :class:`RetryState` before each wait.
        logger: Logger (or logger name) for the per-retry WARNING message.

    Returns:
        The wrapped function, or a decorator when ``func`` is omitted.

    Raises:
        ValueError: If an option is out of range (raised when decorating).

    Example:
        >>> @retry(max_attempts=5, delay=0.5, backoff=2, on=ConnectionError)
        ... def fetch() -> str: ...
    """
    _validate(max_attempts, delay, backoff, max_delay, jitter)

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        name = _core.qualname(f)
        log = _core.get_logger(logger)

        def plan(attempt: int, exc: BaseException, started: float) -> float | None:
            """Return the wait before the next attempt, or None to re-raise."""
            if retry_if is not None and not retry_if(exc):
                return None
            if max_attempts is not None and attempt >= max_attempts:
                return None
            wait = _compute_delay(attempt, delay, backoff, max_delay, jitter)
            if on_retry is not None:
                on_retry(RetryState(attempt, exc, wait, _core.monotonic() - started))
            total = "inf" if max_attempts is None else str(max_attempts)
            log.warning(
                "%s failed (attempt %d/%s): %r; retrying in %.3fs", name, attempt, total, exc, wait
            )
            return wait

        if _core.is_async(f):
            af = cast("Callable[..., Coroutine[Any, Any, Any]]", f)

            @functools.wraps(af)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                started = _core.monotonic()
                attempt = 1
                while True:
                    try:
                        return await af(*args, **kwargs)
                    except on as exc:
                        wait = plan(attempt, exc, started)
                        if wait is None:
                            raise
                    await _core.async_sleep(wait)
                    attempt += 1

            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(f)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            started = _core.monotonic()
            attempt = 1
            while True:
                try:
                    return f(*args, **kwargs)
                except on as exc:
                    wait = plan(attempt, exc, started)
                    if wait is None:
                        raise
                _core.sleep(wait)
                attempt += 1

        return wrapper

    return decorate if func is None else decorate(func)


def retry_on_exception(
    *exceptions: type[BaseException],
    max_attempts: int | None = 3,
    delay: float = 0.0,
    backoff: float = 1.0,
    max_delay: float | None = None,
    jitter: float = 0.0,
    retry_if: Callable[[BaseException], bool] | None = None,
    on_retry: Callable[[RetryState], None] | None = None,
    logger: LoggerLike = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Deprecated alias for ``retry(on=exceptions or Exception, ...)``.

    Args:
        *exceptions: Exception types to retry on (default ``Exception``).
        max_attempts: Total calls allowed, or ``None`` for no limit.
        delay: Base wait in seconds between attempts.
        backoff: Multiplier applied to the wait after each failure.
        max_delay: Upper bound for the wait.
        jitter: Random extra seconds added to each wait.
        retry_if: Extra predicate deciding whether to retry.
        on_retry: Callback invoked before each wait.
        logger: Logger (or name) for retry messages.

    Returns:
        A decorator. Applying it emits a ``DeprecationWarning``.

    Raises:
        ValueError: If an option is out of range.

    Example:
        >>> @retry_on_exception(ValueError)  # use retry(on=ValueError) instead
        ... def parse() -> None: ...
    """
    on: ExceptionTypes = exceptions or Exception
    _validate(max_attempts, delay, backoff, max_delay, jitter)

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        warnings.warn(
            "retry_on_exception is deprecated; use retry(on=...) instead",
            DeprecationWarning,
            stacklevel=2,
        )
        return retry(
            f,
            max_attempts=max_attempts,
            delay=delay,
            backoff=backoff,
            max_delay=max_delay,
            jitter=jitter,
            on=on,
            retry_if=retry_if,
            on_retry=on_retry,
            logger=logger,
        )

    return decorate


class _Decorator(Protocol[T_co]):
    """A decorator that maps ``f`` to ``f`` with ``T_co`` added to the result type."""

    @overload
    def __call__(
        self, func: Callable[P, Coroutine[Any, Any, R]], /
    ) -> Callable[P, Coroutine[Any, Any, R | T_co]]: ...
    @overload
    def __call__(self, func: Callable[P, R], /) -> Callable[P, R | T_co]: ...


def _make_catcher(
    on: ExceptionTypes,
    on_error: Callable[[Callable[..., Any], BaseException], Any],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(f: Callable[..., Any]) -> Callable[..., Any]:
        if _core.is_async(f):

            @functools.wraps(f)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    return await f(*args, **kwargs)
                except on as exc:
                    return on_error(f, exc)

            return async_wrapper

        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return f(*args, **kwargs)
            except on as exc:
                return on_error(f, exc)

        return wrapper

    return decorate


@overload
def fallback(
    value: T, /, *, on: ExceptionTypes = ..., logger: LoggerLike = ...
) -> _Decorator[T]: ...
@overload
def fallback(
    *, handler: Callable[[BaseException], T], on: ExceptionTypes = ..., logger: LoggerLike = ...
) -> _Decorator[T]: ...
def fallback(
    value: Any = _MISSING,
    /,
    *,
    handler: Callable[[BaseException], Any] | None = None,
    on: ExceptionTypes = Exception,
    logger: LoggerLike = None,
) -> Any:
    """Return a default value (or a handler's result) when the function raises.

    Args:
        value: The value to return on a matching exception.
        handler: Alternatively, a callable ``handler(exc)`` whose result is returned.
        on: Exception type(s) that trigger the fallback.
        logger: Logger (or name) for the WARNING message (logged with traceback).

    Returns:
        A decorator for sync or async functions. For async functions the handler is
        still a regular (sync) function.

    Raises:
        ValueError: If neither or both of ``value`` and ``handler`` are given.

    Example:
        >>> @fallback(0, on=KeyError)
        ... def lookup(key: str) -> int: ...
    """
    if (value is _MISSING) == (handler is None):
        raise ValueError("fallback requires exactly one of `value` or `handler`")
    log = _core.get_logger(logger)

    def on_error(f: Callable[..., Any], exc: BaseException) -> Any:
        log.warning(
            "%s raised %r; using fallback",
            _core.qualname(f),
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
        return handler(exc) if handler is not None else value

    return _make_catcher(on, on_error)


@overload
def suppress(  # type: ignore[overload-overlap]
    exc: type[BaseException], /, *more: type[BaseException], logger: LoggerLike = ...
) -> _Decorator[None]: ...
@overload
def suppress(
    func: Callable[P, Coroutine[Any, Any, R]], /
) -> Callable[P, Coroutine[Any, Any, R | None]]: ...
@overload
def suppress(func: Callable[P, R], /) -> Callable[P, R | None]: ...
@overload
def suppress(*, logger: LoggerLike = ...) -> _Decorator[None]: ...
def suppress(*exceptions: Any, logger: LoggerLike = None) -> Any:
    """Swallow matching exceptions; the call then returns ``None``.

    Usable bare (``@suppress``, which suppresses ``Exception``) or with exception
    types (``@suppress(KeyError)``). Suppressed errors are logged at DEBUG.

    Args:
        *exceptions: Exception types to swallow (default ``Exception``).
        logger: Logger (or name) for the DEBUG message.

    Returns:
        The wrapped function, or a decorator when exception types are given.

    Example:
        >>> @suppress(KeyError)
        ... def first(d: dict[str, int]) -> int: ...
    """
    log = _core.get_logger(logger)

    def make(on: ExceptionTypes) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def on_error(f: Callable[..., Any], exc: BaseException) -> None:
            log.debug("%s raised %r; suppressed", _core.qualname(f), exc)

        return _make_catcher(on, on_error)

    if len(exceptions) == 1 and not isinstance(exceptions[0], type) and callable(exceptions[0]):
        return make(Exception)(exceptions[0])
    return make(tuple(exceptions) or Exception)
