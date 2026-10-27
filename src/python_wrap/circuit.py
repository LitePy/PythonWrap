"""Circuit breaker decorator and class."""

from __future__ import annotations

import enum
import functools
import threading
from collections.abc import Callable, Coroutine
from typing import Any, cast, overload

from python_wrap import _core
from python_wrap._core import LoggerLike, P, R
from python_wrap.errors import CircuitOpenError

__all__ = ["CircuitBreaker", "CircuitState", "circuit_breaker"]

ExceptionTypes = type[BaseException] | tuple[type[BaseException], ...]


class CircuitState(str, enum.Enum):
    """The three states of a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Stop calling a failing dependency until it has had time to recover.

    The breaker is closed at first. After ``failure_threshold`` consecutive matching
    failures it opens and rejects calls with :class:`~python_wrap.errors.CircuitOpenError`.
    After ``recovery_timeout`` seconds it goes half-open and lets up to
    ``half_open_max_calls`` trial calls through: a success closes it, a failure
    re-opens it. One instance can decorate several functions to share state.

    Args:
        failure_threshold: Consecutive failures that open the circuit (>= 1).
        recovery_timeout: Seconds to stay open before allowing trial calls.
        half_open_max_calls: Concurrent trial calls allowed while half-open (>= 1).
        on: Exception type(s) that count as failures; others pass through uncounted.
        name: Name used in log messages.
        logger: Logger (or name) for state transitions.

    Raises:
        ValueError: If a numeric option is out of range.

    Example:
        >>> breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=10)
        >>> @breaker
        ... def call_api() -> None: ...
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
        on: ExceptionTypes = Exception,
        name: str | None = None,
        logger: LoggerLike = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >= 1, got {failure_threshold!r}")
        if recovery_timeout < 0:
            raise ValueError(f"recovery_timeout must be >= 0, got {recovery_timeout!r}")
        if half_open_max_calls < 1:
            raise ValueError(f"half_open_max_calls must be >= 1, got {half_open_max_calls!r}")
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.on = on
        self.name = name or "circuit"
        self._log = _core.get_logger(logger)
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trials = 0
        self._generation = 0

    # -- introspection -------------------------------------------------------------
    @property
    def state(self) -> CircuitState:
        """Current state (an open circuit past its timeout reports ``half_open``)."""
        with self._lock:
            self._maybe_half_open()
            return self._state

    @property
    def failure_count(self) -> int:
        """Consecutive failures counted so far."""
        with self._lock:
            return self._failures

    def reset(self) -> None:
        """Force the breaker back to closed with a zero failure count."""
        with self._lock:
            if self._state is not CircuitState.CLOSED:
                self._log.info("circuit %s reset to closed", self.name)
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._trials = 0
            self._generation += 1

    # -- internals (call with the lock held) ---------------------------------------
    def _maybe_half_open(self) -> None:
        if (
            self._state is CircuitState.OPEN
            and _core.monotonic() - self._opened_at >= self.recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            self._trials = 0
            self._generation += 1
            self._log.info("circuit %s is half-open; allowing trial calls", self.name)

    def _open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = _core.monotonic()
        self._trials = 0
        self._generation += 1
        self._log.warning(
            "circuit %s opened after %d failure(s); rejecting calls for %.1fs",
            self.name,
            self._failures,
            self.recovery_timeout,
        )

    def _acquire(self) -> tuple[int, bool]:
        """Admit a call. Returns ``(generation, is_trial)`` or raises CircuitOpenError."""
        with self._lock:
            self._maybe_half_open()
            if self._state is CircuitState.OPEN:
                remaining = self.recovery_timeout - (_core.monotonic() - self._opened_at)
                raise CircuitOpenError(
                    f"circuit {self.name} is open; retry in {remaining:.1f}s",
                    retry_after=max(remaining, 0.0),
                )
            if self._state is CircuitState.HALF_OPEN:
                if self._trials >= self.half_open_max_calls:
                    raise CircuitOpenError(
                        f"circuit {self.name} is half-open and its trial calls are in use",
                        retry_after=0.0,
                    )
                self._trials += 1
                return self._generation, True
            return self._generation, False

    def _success(self, generation: int, trial: bool) -> None:
        with self._lock:
            if generation != self._generation:
                return
            if trial:
                self._state = CircuitState.CLOSED
                self._trials = 0
                self._generation += 1
                self._log.info("circuit %s closed after a successful trial call", self.name)
            self._failures = 0

    def _failure(self, generation: int, trial: bool) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._failures += 1
            if trial or self._failures >= self.failure_threshold:
                self._open()

    def _release(self, generation: int, trial: bool) -> None:
        """A non-counted exception ended the call: free its trial slot, if any."""
        if not trial:
            return
        with self._lock:
            if generation == self._generation:
                self._trials = max(self._trials - 1, 0)

    # -- public call API -----------------------------------------------------------
    @overload
    def call(
        self, func: Callable[P, Coroutine[Any, Any, R]], /, *args: P.args, **kwargs: P.kwargs
    ) -> Coroutine[Any, Any, R]: ...
    @overload
    def call(self, func: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R: ...
    def call(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Call ``func(*args, **kwargs)`` through the breaker.

        For an ``async def`` function this returns a coroutine to await.

        Args:
            func: The function to call.
            *args: Positional arguments for ``func``.
            **kwargs: Keyword arguments for ``func``.

        Returns:
            Whatever ``func`` returns (a coroutine if it is async).

        Raises:
            CircuitOpenError: If the circuit is open (``func`` is not called).
        """
        if _core.is_async(func):
            return self._acall(func, args, kwargs)
        generation, trial = self._acquire()
        try:
            result = func(*args, **kwargs)
        except self.on:
            self._failure(generation, trial)
            raise
        except BaseException:
            self._release(generation, trial)
            raise
        self._success(generation, trial)
        return result

    async def _acall(self, func: Callable[..., Any], args: Any, kwargs: Any) -> Any:
        # The lock is only taken inside the sync helpers, never across the await.
        generation, trial = self._acquire()
        try:
            result = await func(*args, **kwargs)
        except self.on:
            self._failure(generation, trial)
            raise
        except BaseException:
            self._release(generation, trial)
            raise
        self._success(generation, trial)
        return result

    def __call__(self, func: Callable[P, R]) -> Callable[P, R]:
        """Decorate ``func`` so every call goes through this breaker."""
        if _core.is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await self._acall(func, args, kwargs)

            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return self.call(func, *args, **kwargs)

        return wrapper

    def __repr__(self) -> str:
        return f"CircuitBreaker(name={self.name!r}, state={self.state.value!r})"


@overload
def circuit_breaker(
    func: Callable[P, R],
    /,
    *,
    failure_threshold: int = ...,
    recovery_timeout: float = ...,
    half_open_max_calls: int = ...,
    on: ExceptionTypes = ...,
    name: str | None = ...,
    logger: LoggerLike = ...,
) -> Callable[P, R]: ...
@overload
def circuit_breaker(
    func: None = None,
    /,
    *,
    failure_threshold: int = ...,
    recovery_timeout: float = ...,
    half_open_max_calls: int = ...,
    on: ExceptionTypes = ...,
    name: str | None = ...,
    logger: LoggerLike = ...,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...
def circuit_breaker(
    func: Callable[P, R] | None = None,
    /,
    *,
    failure_threshold: int = 5,
    recovery_timeout: float = 30.0,
    half_open_max_calls: int = 1,
    on: ExceptionTypes = Exception,
    name: str | None = None,
    logger: LoggerLike = None,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate a function with its own dedicated :class:`CircuitBreaker`.

    The wrapper exposes the breaker as ``wrapper.breaker``. To share one breaker
    between several functions, create a :class:`CircuitBreaker` and use it as a
    decorator instead.

    Args:
        func: The function to wrap (when used without parentheses).
        failure_threshold: Consecutive failures that open the circuit.
        recovery_timeout: Seconds to stay open before allowing trial calls.
        half_open_max_calls: Trial calls allowed while half-open.
        on: Exception type(s) counted as failures.
        name: Name for log messages (defaults to the function's qualified name).
        logger: Logger (or name) for state transitions.

    Returns:
        The wrapped function, or a decorator when ``func`` is omitted.

    Raises:
        ValueError: If a numeric option is out of range.

    Example:
        >>> @circuit_breaker(failure_threshold=3, recovery_timeout=10)
        ... def call_api() -> None: ...
    """

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            half_open_max_calls=half_open_max_calls,
            on=on,
            name=name or _core.qualname(f),
            logger=logger,
        )
        wrapped = breaker(f)
        wrapped.breaker = breaker  # type: ignore[attr-defined]
        return wrapped

    return decorate if func is None else decorate(func)
