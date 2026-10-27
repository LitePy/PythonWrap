"""Retry, fallback and suppress decorators."""

from __future__ import annotations

import asyncio
import datetime
import email.utils
import functools
import logging
import math
import random
import sys
import threading
import time
import warnings
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, TypeVar, cast, overload

from python_wrap import _core
from python_wrap._core import F, LoggerLike, P, R

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)

ExceptionTypes = type[BaseException] | tuple[type[BaseException], ...]

__all__ = [
    "Attempt",
    "RetryState",
    "RetryStats",
    "Retrying",
    "fallback",
    "retry",
    "retry_after_header",
    "retry_on_exception",
    "retry_stats",
    "retrying",
    "suppress",
]

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
    max_attempts: int | None,
    delay: float,
    backoff: float,
    max_delay: float | None,
    jitter: float,
    max_time: float | None = None,
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
    if max_time is not None and max_time < 0:
        raise ValueError(f"max_time must be >= 0 or None, got {max_time!r}")


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


class RetryStats:
    """Thread-safe counters for one retried function. Read them with `retry_stats`.

    Attributes:
        calls: Times the wrapped function was called (each call may make several attempts).
        attempts_total: Individual attempts made across all calls.
        retries: Times an attempt failed and another one was scheduled.
        gave_up: Calls that ended by re-raising a retryable exception (attempts, time
            budget or `retry_if` said stop).
        total_sleep: Seconds spent waiting between attempts.
        last_exception: The most recent exception a retried attempt raised, if any.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.attempts_total = 0
        self.retries = 0
        self.gave_up = 0
        self.total_sleep = 0.0
        self.last_exception: BaseException | None = None

    def snapshot(self) -> dict[str, Any]:
        """Return a consistent copy of all counters as a dict."""
        with self._lock:
            return {
                "calls": self.calls,
                "attempts_total": self.attempts_total,
                "retries": self.retries,
                "gave_up": self.gave_up,
                "total_sleep": self.total_sleep,
                "last_exception": self.last_exception,
            }

    def reset(self) -> None:
        """Set every counter back to zero."""
        with self._lock:
            self.calls = self.attempts_total = self.retries = self.gave_up = 0
            self.total_sleep = 0.0
            self.last_exception = None

    def __repr__(self) -> str:
        s = self.snapshot()
        return (
            f"RetryStats(calls={s['calls']}, attempts_total={s['attempts_total']}, "
            f"retries={s['retries']}, gave_up={s['gave_up']}, total_sleep={s['total_sleep']:g})"
        )


_STATS_ATTR = "__retry_stats__"


def retry_stats(func: Callable[..., Any]) -> RetryStats | None:
    """Return the `RetryStats` of a function decorated with `retry`.

    Works through other decorators that use `functools.wraps` and on bound methods
    (the counters are shared by all instances of the class).

    Args:
        func: A function wrapped by `retry`.

    Returns:
        Its statistics, or `None` if `func` is not retried.

    Example:
        >>> @retry
        ... def f() -> None: ...
        >>> retry_stats(f).calls
        0
    """
    stats = getattr(func, _STATS_ATTR, None)
    return stats if isinstance(stats, RetryStats) else None


def _parse_seconds(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value).strip()
        try:
            seconds = float(text)
        except ValueError:
            moment = email.utils.parsedate_to_datetime(text)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
            seconds = moment.timestamp() - time.time()
    if math.isnan(seconds) or math.isinf(seconds):
        return None
    return max(seconds, 0.0)


def retry_after_header(exc: BaseException) -> float | None:
    """Extract a server-requested wait from an exception, for `delay_from=`.

    Looks at `exc.retry_after` first, then at `exc.response.headers["Retry-After"]`
    (header names are matched case-insensitively). Values may be seconds or an HTTP-date;
    dates are converted to seconds from now. Works with `httpx`, `requests` and
    `aiohttp`-style exceptions without importing them, and never raises.

    Args:
        exc: Any exception.

    Returns:
        Seconds to wait (never negative), or `None` if nothing usable was found.

    Example:
        >>> class Err(Exception):
        ...     retry_after = 3
        >>> retry_after_header(Err())
        3.0
    """
    try:
        found = _parse_seconds(getattr(exc, "retry_after", None))
        if found is not None:
            return found
        headers = exc.response.headers  # type: ignore[attr-defined]
        value = headers.get("Retry-After")
        if value is None:
            for key in headers:
                if str(key).lower() == "retry-after":
                    value = headers[key]
                    break
        return _parse_seconds(value)
    except Exception:
        return None


# Never retried, even with ``on=BaseException``: retrying these hangs shutdown/cancellation.
_NEVER_RETRY = (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit)


def _parse_hint(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        return None
    return max(float(value), 0.0)


class _Engine:
    """The retry state machine shared by `retry` and `retrying`."""

    def __init__(
        self,
        name: str,
        *,
        max_attempts: int | None,
        delay: float,
        backoff: float,
        max_delay: float | None,
        jitter: float,
        max_time: float | None,
        on: ExceptionTypes,
        retry_if: Callable[[BaseException], bool] | None,
        on_retry: Callable[[RetryState], None] | None,
        delay_from: Callable[[BaseException], float | None] | None,
        logger: LoggerLike,
        log_level: int | None,
        log_exc_info: bool,
        stats: RetryStats,
    ) -> None:
        _validate(max_attempts, delay, backoff, max_delay, jitter, max_time)
        self.name = name
        self.max_attempts = max_attempts
        self.delay = delay
        self.backoff = backoff
        self.max_delay = max_delay
        self.jitter = jitter
        self.max_time = max_time
        self.on = on
        self.retry_if = retry_if
        self.on_retry = on_retry
        self.delay_from = delay_from
        self.log = _core.get_logger(logger)
        self.log_level = log_level
        self.log_exc_info = log_exc_info
        self.stats = stats

    def limit(self) -> int | None:
        """Attempts allowed for this call, honouring the test override at call time."""
        cap = _core.overrides.max_attempts
        if cap is None:
            return self.max_attempts
        return cap if self.max_attempts is None else min(self.max_attempts, cap)

    def start_call(self) -> float:
        with self.stats._lock:
            self.stats.calls += 1
        return _core.monotonic()

    def start_attempt(self) -> None:
        with self.stats._lock:
            self.stats.attempts_total += 1

    def success(self, attempts: int) -> None:
        _core.emit("retry.succeeded", self.name, attempts=attempts)

    def slept(self, seconds: float) -> None:
        with self.stats._lock:
            self.stats.total_sleep += seconds

    def _wait_for(self, attempt: int, exc: BaseException) -> float:
        if self.delay_from is not None:
            try:
                hint = _parse_hint(self.delay_from(exc))
            except Exception:
                self.log.debug("%s: delay_from raised; using backoff", self.name, exc_info=True)
                hint = None
            if hint is not None:
                return hint if self.max_delay is None else min(hint, self.max_delay)
        return _compute_delay(attempt, self.delay, self.backoff, self.max_delay, self.jitter)

    def failure(self, attempt: int, exc: BaseException, started: float) -> float | None:
        """Decide after a failed attempt: the wait in seconds, or None to re-raise."""
        if isinstance(exc, _NEVER_RETRY):
            return None
        with self.stats._lock:
            self.stats.last_exception = exc
        wait: float | None = None
        limit = self.limit()
        if (self.retry_if is None or self.retry_if(exc)) and (limit is None or attempt < limit):
            wait = self._wait_for(attempt, exc)
            if self.max_time is not None and _core.monotonic() - started + wait > self.max_time:
                wait = None
        if wait is None:
            with self.stats._lock:
                self.stats.gave_up += 1
            _core.emit("retry.gave_up", self.name, attempts=attempt, exception=exc)
            return None
        with self.stats._lock:
            self.stats.retries += 1
        if self.on_retry is not None:
            self.on_retry(RetryState(attempt, exc, wait, _core.monotonic() - started))
        if self.log_level is not None:
            self.log.log(
                self.log_level,
                "%s failed (attempt %d/%s): %r; retrying in %.3fs",
                self.name,
                attempt,
                "inf" if limit is None else limit,
                exc,
                wait,
                exc_info=(type(exc), exc, exc.__traceback__) if self.log_exc_info else None,
            )
        _core.emit(
            "retry.attempt_failed", self.name, attempt=attempt, exception=exc, next_delay=wait
        )
        return wait


@overload
def retry(
    func: F,
    /,
    *,
    max_attempts: int | None = ...,
    delay: float = ...,
    backoff: float = ...,
    max_delay: float | None = ...,
    jitter: float = ...,
    max_time: float | None = ...,
    on: ExceptionTypes = ...,
    retry_if: Callable[[BaseException], bool] | None = ...,
    on_retry: Callable[[RetryState], None] | None = ...,
    delay_from: Callable[[BaseException], float | None] | None = ...,
    logger: LoggerLike = ...,
    log_level: int | None = ...,
    log_exc_info: bool = ...,
) -> F: ...
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
    max_time: float | None = ...,
    on: ExceptionTypes = ...,
    retry_if: Callable[[BaseException], bool] | None = ...,
    on_retry: Callable[[RetryState], None] | None = ...,
    delay_from: Callable[[BaseException], float | None] | None = ...,
    logger: LoggerLike = ...,
    log_level: int | None = ...,
    log_exc_info: bool = ...,
) -> Callable[[F], F]: ...
def retry(
    func: Callable[..., Any] | None = None,
    /,
    *,
    max_attempts: int | None = 3,
    delay: float = 1.0,
    backoff: float = 1.0,
    max_delay: float | None = None,
    jitter: float = 0.0,
    max_time: float | None = None,
    on: ExceptionTypes = Exception,
    retry_if: Callable[[BaseException], bool] | None = None,
    on_retry: Callable[[RetryState], None] | None = None,
    delay_from: Callable[[BaseException], float | None] | None = None,
    logger: LoggerLike = None,
    log_level: int | None = logging.WARNING,
    log_exc_info: bool = False,
) -> Any:
    """Retry a function when it raises, optionally with exponential backoff.

    Works bare (`@retry`), with options (`@retry(max_attempts=5)`) and as a plain
    call (`retry(func, max_attempts=5)`). Async functions are awaited and slept
    with `asyncio` so the event loop is never blocked.

    The wait before attempt `n + 1` is
    `min(delay * backoff ** (n - 1), max_delay) + random.uniform(0, jitter)`.
    The last exception is re-raised unchanged, with its original traceback, once
    attempts or the time budget are exhausted.

    `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit` and `GeneratorExit` are
    never retried, even with `on=BaseException` or when listed in `on`. Cancelling an
    async call during the backoff sleep cancels it immediately.

    Statistics are available through `retry_stats`. The events `retry.attempt_failed`,
    `retry.succeeded` and `retry.gave_up` are emitted for `python_wrap.events`. Under
    `python_wrap.testing`, an `overrides.max_attempts` cap lowers `max_attempts`.

    Args:
        func: The function to wrap (when used without parentheses).
        max_attempts: Total number of calls allowed, or `None` for no limit.
        delay: Base wait in seconds before the second attempt.
        backoff: Multiplier applied to the wait after each failure (>= 1).
        max_delay: Upper bound for the backoff wait (before jitter is added); also caps
            waits returned by `delay_from`.
        jitter: Up to this many random extra seconds are added to each wait.
        max_time: Total time budget in seconds, measured from the first attempt. If the
            next wait would run past it, the last exception is re-raised instead.
        on: Exception type(s) that trigger a retry; anything else propagates at once.
        retry_if: Extra predicate; the exception is retried only if it returns True.
        on_retry: Called with a `RetryState` before each wait.
        delay_from: Called with the exception; a number is the exact wait for this
            attempt (no backoff, no jitter; still capped by `max_delay` and `max_time`),
            `None` means use the normal backoff. See `retry_after_header`.
        logger: Logger (or logger name) for the per-retry message.
        log_level: Level of the per-retry message, or `None` to not log.
        log_exc_info: Attach the traceback to the per-retry log record.

    Returns:
        The wrapped function, or a decorator when `func` is omitted.

    Raises:
        ValueError: If an option is out of range (raised when decorating).

    Example:
        >>> @retry(max_attempts=5, delay=0.5, backoff=2, on=ConnectionError)
        ... def fetch() -> str: ...
    """
    # Validate eagerly so a bad option fails at decoration time, even with no function yet.
    _validate(max_attempts, delay, backoff, max_delay, jitter, max_time)

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        stats = RetryStats()
        engine = _Engine(
            _core.qualname(f),
            max_attempts=max_attempts,
            delay=delay,
            backoff=backoff,
            max_delay=max_delay,
            jitter=jitter,
            max_time=max_time,
            on=on,
            retry_if=retry_if,
            on_retry=on_retry,
            delay_from=delay_from,
            logger=logger,
            log_level=log_level,
            log_exc_info=log_exc_info,
            stats=stats,
        )

        if _core.is_async(f):
            af = cast("Callable[..., Coroutine[Any, Any, Any]]", f)

            @functools.wraps(af)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                started = engine.start_call()
                attempt = 1
                while True:
                    engine.start_attempt()
                    try:
                        result = await af(*args, **kwargs)
                    except on as exc:
                        wait = engine.failure(attempt, exc, started)
                        if wait is None:
                            raise
                    else:
                        engine.success(attempt)
                        return result
                    await _core.async_sleep(wait)
                    engine.slept(wait)
                    attempt += 1

            setattr(async_wrapper, _STATS_ATTR, stats)
            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(f)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            started = engine.start_call()
            attempt = 1
            while True:
                engine.start_attempt()
                try:
                    result = f(*args, **kwargs)
                except on as exc:
                    wait = engine.failure(attempt, exc, started)
                    if wait is None:
                        raise
                else:
                    engine.success(attempt)
                    return result
                _core.sleep(wait)
                engine.slept(wait)
                attempt += 1

        setattr(wrapper, _STATS_ATTR, stats)
        return wrapper

    return decorate if func is None else decorate(func)


class _Run:
    def __init__(self, engine: _Engine) -> None:
        self.engine = engine
        self.started = engine.start_call()


class Attempt:
    """One attempt of a `retrying` loop; use it as a context manager.

    Exceptions that `retry` would retry are swallowed by `with attempt:` (the loop then
    waits and continues); other exceptions, and the failure of the final attempt,
    propagate.

    Attributes:
        number: 1-based number of this attempt.
    """

    def __init__(self, number: int, run: _Run) -> None:
        self.number = number
        self._run = run
        self._wait: float | None = None

    def __enter__(self) -> Attempt:
        self._run.engine.start_attempt()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        engine = self._run.engine
        if exc is None or not isinstance(exc, engine.on):
            return False
        self._wait = engine.failure(self.number, exc, self._run.started)
        return self._wait is not None


class Retrying:
    """Iterable of `Attempt` objects returned by `retrying`.

    Iterate it with `for` (sync, sleeps with `time.sleep`) or `async for` (sleeps with
    `asyncio`). Each iteration over a `Retrying` is an independent run.
    """

    def __init__(self, engine: _Engine) -> None:
        self._engine = engine

    @property
    def stats(self) -> RetryStats:
        """Counters accumulated over all runs of this object."""
        return self._engine.stats

    def __iter__(self) -> Iterator[Attempt]:
        run = _Run(self._engine)
        number = 1
        while True:
            attempt = Attempt(number, run)
            yield attempt
            wait = attempt._wait
            if wait is None:
                run.engine.success(number)
                return
            _core.sleep(wait)
            run.engine.slept(wait)
            number += 1

    async def __aiter__(self) -> AsyncIterator[Attempt]:
        run = _Run(self._engine)
        number = 1
        while True:
            attempt = Attempt(number, run)
            yield attempt
            wait = attempt._wait
            if wait is None:
                run.engine.success(number)
                return
            await _core.async_sleep(wait)
            run.engine.slept(wait)
            number += 1


def retrying(
    *,
    max_attempts: int | None = 3,
    delay: float = 1.0,
    backoff: float = 1.0,
    max_delay: float | None = None,
    jitter: float = 0.0,
    max_time: float | None = None,
    on: ExceptionTypes = Exception,
    retry_if: Callable[[BaseException], bool] | None = None,
    on_retry: Callable[[RetryState], None] | None = None,
    delay_from: Callable[[BaseException], float | None] | None = None,
    logger: LoggerLike = None,
    log_level: int | None = logging.WARNING,
    log_exc_info: bool = False,
    name: str = "retrying",
) -> Retrying:
    """Retry an inline block of code instead of a whole function.

    Takes the same options as `retry` and behaves identically (backoff, jitter,
    `max_time`, `delay_from`, cancellation rules, events, test overrides). Put the
    risky code in `with attempt:`: a retryable exception is swallowed and the loop
    waits and continues; success ends the loop; the final failure propagates.

    Use `for` in sync code and `async for` in async code. Do not use `for` inside a
    coroutine, it would block the event loop while waiting.

    Args:
        max_attempts: Total attempts allowed, or `None` for no limit.
        delay: Base wait in seconds before the second attempt.
        backoff: Multiplier applied to the wait after each failure (>= 1).
        max_delay: Upper bound for the backoff wait (before jitter).
        jitter: Up to this many random extra seconds are added to each wait.
        max_time: Total time budget in seconds from the first attempt.
        on: Exception type(s) that trigger a retry.
        retry_if: Extra predicate; retry only if it returns True.
        on_retry: Called with a `RetryState` before each wait.
        delay_from: Exact wait derived from the exception, or `None` for backoff.
        logger: Logger (or logger name) for the per-retry message.
        log_level: Level of the per-retry message, or `None` to not log.
        log_exc_info: Attach the traceback to the per-retry log record.
        name: Name used in log messages and events.

    Returns:
        A `Retrying` object to iterate over.

    Raises:
        ValueError: If an option is out of range.

    Example:
        >>> for attempt in retrying(max_attempts=3, delay=0.1, on=ConnectionError):
        ...     with attempt:
        ...         do_work()  # doctest: +SKIP
    """
    return Retrying(
        _Engine(
            name,
            max_attempts=max_attempts,
            delay=delay,
            backoff=backoff,
            max_delay=max_delay,
            jitter=jitter,
            max_time=max_time,
            on=on,
            retry_if=retry_if,
            on_retry=on_retry,
            delay_from=delay_from,
            logger=logger,
            log_level=log_level,
            log_exc_info=log_exc_info,
            stats=RetryStats(),
        )
    )


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
) -> Callable[[F], F]:
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

    return cast("Callable[[F], F]", decorate)


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
