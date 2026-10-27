"""Circuit breaker decorator and class."""

from __future__ import annotations

import enum
import functools
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Concatenate, ParamSpec, Protocol, TypeVar, cast, overload

from python_wrap import _core
from python_wrap._core import F, LoggerLike, P, R
from python_wrap.errors import CircuitOpenError

__all__ = [
    "CircuitBreaker",
    "CircuitProtected",
    "CircuitState",
    "CircuitStats",
    "StateChangeListener",
    "circuit_breaker",
]

R_co = TypeVar("R_co", covariant=True)
P2 = ParamSpec("P2")
T = TypeVar("T")

ExceptionTypes = type[BaseException] | tuple[type[BaseException], ...]


class CircuitState(str, enum.Enum):
    """The three states of a circuit breaker."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


StateChangeListener = Callable[["CircuitBreaker", CircuitState, CircuitState], None]
"""Signature of ``on_state_change``: ``listener(breaker, old_state, new_state)``."""


@dataclass(frozen=True)
class CircuitStats:
    """A point-in-time snapshot of a `CircuitBreaker`, from its ``.stats`` property.

    Attributes:
        name: The breaker's name.
        state: Current state.
        failures: Calls that failed with a counted exception (since creation or the last
            ``reset()``).
        successes: Calls that returned normally (same period).
        rejected: Calls refused with `CircuitOpenError` (same period).
        consecutive_failures: Counted failures since the last success.
        opened_at: ``_core.monotonic()`` time at which the circuit last opened, or
            ``None`` while it is closed.
    """

    name: str
    state: CircuitState
    failures: int
    successes: int
    rejected: int
    consecutive_failures: int
    opened_at: float | None


class CircuitBreaker:
    """Stop calling a failing dependency until it has had time to recover.

    The breaker is closed at first. After ``failure_threshold`` consecutive matching
    failures it opens and rejects calls with `CircuitOpenError`.
    After ``recovery_timeout`` seconds it goes half-open and lets up to
    ``half_open_max_calls`` trial calls run at once: ``success_threshold`` consecutive
    successful trials close it, any counted failure re-opens it. One instance can
    decorate several functions to share state.

    Exceptions matching ``on`` are counted as failures, unless they also match
    ``exclude``. Excluded and non-matching exceptions propagate to the caller without
    affecting the breaker (they count neither as a failure nor as a success).

    Each transition calls ``on_state_change(breaker, old, new)`` (outside the breaker's
    lock; a failing listener is logged, never raised) and emits the
    ``circuit.state_changed`` event. Every rejected call emits ``circuit.rejected``.

    Args:
        failure_threshold: Consecutive failures that open the circuit (>= 1).
        recovery_timeout: Seconds to stay open before allowing trial calls.
        half_open_max_calls: Concurrent trial calls allowed while half-open (>= 1).
        success_threshold: Consecutive successful trial calls needed to close a
            half-open circuit (>= 1).
        on: Exception type(s) that count as failures; others pass through uncounted.
        exclude: Exception type(s) never counted, even when they match ``on``, e.g.
            ``exclude=NotFound`` with ``on=Exception``.
        on_state_change: Listener called with ``(breaker, old_state, new_state)``.
        name: Name used in log messages, errors and events.
        logger: Logger (or name) for state transitions.

    Raises:
        ValueError: If a numeric option is out of range.

    Example:
        ```python
        from python_wrap.circuit import CircuitBreaker, CircuitState

        changes = []
        breaker = CircuitBreaker(
            failure_threshold=2,
            exclude=KeyError,
            on_state_change=lambda b, old, new: changes.append((old.value, new.value)),
            name="payments",
        )

        @breaker
        def charge(fail: bool) -> str:
            if fail:
                raise ConnectionError("down")
            return "ok"

        for _ in range(2):
            try:
                charge(True)
            except ConnectionError:
                pass
        assert breaker.state is CircuitState.OPEN
        assert changes == [("closed", "open")]
        assert breaker.stats.failures == 2
        ```
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_max_calls: int = 1,
        success_threshold: int = 1,
        on: ExceptionTypes = Exception,
        exclude: ExceptionTypes = (),
        on_state_change: StateChangeListener | None = None,
        name: str | None = None,
        logger: LoggerLike = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError(f"failure_threshold must be >= 1, got {failure_threshold!r}")
        if recovery_timeout < 0:
            raise ValueError(f"recovery_timeout must be >= 0, got {recovery_timeout!r}")
        if half_open_max_calls < 1:
            raise ValueError(f"half_open_max_calls must be >= 1, got {half_open_max_calls!r}")
        if success_threshold < 1:
            raise ValueError(f"success_threshold must be >= 1, got {success_threshold!r}")
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.success_threshold = success_threshold
        self.on = on
        self.exclude = exclude
        self.on_state_change = on_state_change
        self.name = name or "circuit"
        self._log = _core.get_logger(logger)
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trials = 0
        self._trial_successes = 0
        self._generation = 0
        self._total_failures = 0
        self._total_successes = 0
        self._total_rejected = 0
        # Transitions made under the lock, delivered to listeners after releasing it.
        self._pending: list[tuple[CircuitState, CircuitState]] = []

    # -- introspection -------------------------------------------------------------
    @property
    def state(self) -> CircuitState:
        """Current state (an open circuit past its timeout reports ``half_open``)."""
        with self._lock:
            self._maybe_half_open()
            state = self._state
        self._notify()
        return state

    @property
    def failure_count(self) -> int:
        """Consecutive failures counted so far."""
        with self._lock:
            return self._failures

    @property
    def stats(self) -> CircuitStats:
        """A consistent `CircuitStats` snapshot of the counters and state."""
        with self._lock:
            self._maybe_half_open()
            snapshot = CircuitStats(
                name=self.name,
                state=self._state,
                failures=self._total_failures,
                successes=self._total_successes,
                rejected=self._total_rejected,
                consecutive_failures=self._failures,
                opened_at=None if self._state is CircuitState.CLOSED else self._opened_at,
            )
        self._notify()
        return snapshot

    def reset(self) -> None:
        """Force the breaker back to closed and zero every counter."""
        with self._lock:
            if self._state is not CircuitState.CLOSED:
                self._log.info("circuit %s reset to closed", self.name)
                self._set_state(CircuitState.CLOSED)
            self._failures = 0
            self._trials = 0
            self._trial_successes = 0
            self._total_failures = 0
            self._total_successes = 0
            self._total_rejected = 0
            self._generation += 1
        self._notify()

    # -- internals (call with the lock held) ---------------------------------------
    def _set_state(self, new: CircuitState) -> None:
        old = self._state
        self._state = new
        if old is not new:
            self._pending.append((old, new))

    def _maybe_half_open(self) -> None:
        if (
            self._state is CircuitState.OPEN
            and _core.monotonic() - self._opened_at >= self.recovery_timeout
        ):
            self._set_state(CircuitState.HALF_OPEN)
            self._trials = 0
            self._trial_successes = 0
            self._generation += 1
            self._log.info("circuit %s is half-open; allowing trial calls", self.name)

    def _open(self) -> None:
        self._set_state(CircuitState.OPEN)
        self._opened_at = _core.monotonic()
        self._trials = 0
        self._trial_successes = 0
        self._generation += 1
        self._log.warning(
            "circuit %s opened after %d failure(s); rejecting calls for %.1fs",
            self.name,
            self._failures,
            self.recovery_timeout,
        )

    # -- internals (call WITHOUT the lock) -----------------------------------------
    def _notify(self) -> None:
        """Deliver pending transitions to the listener and the event hook."""
        if not self._pending:
            return
        with self._lock:
            pending, self._pending = self._pending, []
        for old, new in pending:
            _core.emit("circuit.state_changed", self.name, old=old, new=new)
            listener = self.on_state_change
            if listener is None:
                continue
            try:
                listener(self, old, new)
            except Exception:
                self._log.exception("circuit %s on_state_change listener failed", self.name)

    def _acquire(self) -> tuple[int, bool]:
        """Admit a call. Returns ``(generation, is_trial)`` or raises CircuitOpenError."""
        error: CircuitOpenError | None = None
        with self._lock:
            self._maybe_half_open()
            admitted = (self._generation, self._state is CircuitState.HALF_OPEN)
            if self._state is CircuitState.OPEN:
                remaining = max(self.recovery_timeout - (_core.monotonic() - self._opened_at), 0)
                error = CircuitOpenError(
                    f"circuit {self.name} is open; retry in {remaining:.1f}s",
                    retry_after=remaining,
                )
            elif self._state is CircuitState.HALF_OPEN:
                if self._trials >= self.half_open_max_calls:
                    error = CircuitOpenError(
                        f"circuit {self.name} is half-open and its trial calls are in use",
                        retry_after=0.0,
                    )
                else:
                    self._trials += 1
            if error is not None:
                self._total_rejected += 1
        self._notify()
        if error is not None:
            _core.emit("circuit.rejected", self.name, retry_after=error.retry_after)
            raise error
        return admitted

    def _success(self, generation: int, trial: bool) -> None:
        with self._lock:
            self._total_successes += 1
            if generation != self._generation:
                return
            if trial:
                self._trials = max(self._trials - 1, 0)
                self._trial_successes += 1
                if self._trial_successes >= self.success_threshold:
                    self._set_state(CircuitState.CLOSED)
                    self._trials = 0
                    self._trial_successes = 0
                    self._generation += 1
                    self._log.info(
                        "circuit %s closed after %d successful trial call(s)",
                        self.name,
                        self.success_threshold,
                    )
            self._failures = 0
        self._notify()

    def _failure(self, generation: int, trial: bool) -> None:
        with self._lock:
            self._total_failures += 1
            if generation != self._generation:
                return
            self._failures += 1
            if trial or self._failures >= self.failure_threshold:
                self._open()
        self._notify()

    def _release(self, generation: int, trial: bool) -> None:
        """A non-counted exception ended the call: free its trial slot, if any."""
        if not trial:
            return
        with self._lock:
            if generation == self._generation:
                self._trials = max(self._trials - 1, 0)

    def _settle(self, generation: int, trial: bool, exc: BaseException) -> None:
        """Book a call that raised ``exc``: a failure, or uncounted."""
        if isinstance(exc, self.on) and not isinstance(exc, self.exclude):
            self._failure(generation, trial)
        else:
            self._release(generation, trial)

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
        return self._call_sync(func, args, kwargs)

    def _call_sync(self, func: Callable[..., Any], args: Any, kwargs: Any) -> Any:
        generation, trial = self._acquire()
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            self._settle(generation, trial, exc)
            raise
        self._success(generation, trial)
        return result

    async def _acall(self, func: Callable[..., Any], args: Any, kwargs: Any) -> Any:
        # The lock is only taken inside the sync helpers, never across the await.
        generation, trial = self._acquire()
        try:
            result = await func(*args, **kwargs)
        except BaseException as exc:
            self._settle(generation, trial, exc)
            raise
        self._success(generation, trial)
        return result

    def __call__(self, func: F) -> F:
        """Decorate ``func`` so every call goes through this breaker.

        The wrapper keeps the exact type of ``func``, so attributes added by decorators
        applied underneath (such as ``cache_info()`` from ``memoize``) stay visible.
        """
        if _core.is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await self._acall(func, args, kwargs)

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Sync vs async was decided above, once: no per-call detection.
            return self._call_sync(func, args, kwargs)

        return cast(F, wrapper)

    def __repr__(self) -> str:
        return f"CircuitBreaker(name={self.name!r}, state={self.state.value!r})"


class CircuitProtected(Protocol[P, R_co]):
    """A function wrapped by `circuit_breaker`: callable, with its own breaker attached.

    Use it to annotate a decorated function, e.g. ``CircuitProtected[[int], str]``.
    Binds ``self`` and ``cls`` like `python_wrap.caching.Memoized`, with the same typing
    limitations.
    """

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...

    @overload
    def __get__(
        self: CircuitProtected[Concatenate[type[T], P2], R_co], instance: None, owner: type[T]
    ) -> _CircuitProtectedOnClass[P, P2, R_co]: ...
    @overload
    def __get__(self, instance: None, owner: type | None = None) -> CircuitProtected[P, R_co]: ...
    @overload
    def __get__(
        self: CircuitProtected[Concatenate[Any, P2], R_co],
        instance: object,
        owner: type | None = None,
    ) -> CircuitProtected[P2, R_co]: ...

    @property
    def breaker(self) -> CircuitBreaker:
        """The dedicated `CircuitBreaker` (inspect ``.state`` or call ``.reset()``)."""
        ...


class _CircuitProtectedOnClass(CircuitProtected[P, R_co], Protocol[P, P2, R_co]):
    """`CircuitProtected` read from a class: accepts the call with and without the first argument.

    Returned for class access (``Cls.attr``) when the first parameter could be ``cls``.
    pyright only picks it under ``@classmethod``; mypy cannot tell a classmethod from an
    instance or static method there, so both call forms are accepted.
    """

    @overload
    def __call__(self, *args: P2.args, **kwargs: P2.kwargs) -> R_co: ...
    @overload
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...


@overload
def circuit_breaker(
    func: Callable[P, R],
    /,
    *,
    failure_threshold: int = ...,
    recovery_timeout: float = ...,
    half_open_max_calls: int = ...,
    success_threshold: int = ...,
    on: ExceptionTypes = ...,
    exclude: ExceptionTypes = ...,
    on_state_change: StateChangeListener | None = ...,
    name: str | None = ...,
    logger: LoggerLike = ...,
) -> CircuitProtected[P, R]: ...
@overload
def circuit_breaker(
    func: None = None,
    /,
    *,
    failure_threshold: int = ...,
    recovery_timeout: float = ...,
    half_open_max_calls: int = ...,
    success_threshold: int = ...,
    on: ExceptionTypes = ...,
    exclude: ExceptionTypes = ...,
    on_state_change: StateChangeListener | None = ...,
    name: str | None = ...,
    logger: LoggerLike = ...,
) -> Callable[[Callable[P, R]], CircuitProtected[P, R]]: ...
def circuit_breaker(
    func: Callable[..., Any] | None = None,
    /,
    *,
    failure_threshold: int = 5,
    recovery_timeout: float = 30.0,
    half_open_max_calls: int = 1,
    success_threshold: int = 1,
    on: ExceptionTypes = Exception,
    exclude: ExceptionTypes = (),
    on_state_change: StateChangeListener | None = None,
    name: str | None = None,
    logger: LoggerLike = None,
) -> Any:
    """Decorate a function with its own dedicated `CircuitBreaker`.

    The wrapper exposes the breaker as ``wrapper.breaker``. To share one breaker
    between several functions, create a `CircuitBreaker` and use it as a
    decorator instead.

    Args:
        func: The function to wrap (when used without parentheses).
        failure_threshold: Consecutive failures that open the circuit.
        recovery_timeout: Seconds to stay open before allowing trial calls.
        half_open_max_calls: Trial calls allowed while half-open.
        success_threshold: Consecutive successful trials needed to close again.
        on: Exception type(s) counted as failures.
        exclude: Exception type(s) never counted, even when they match ``on``.
        on_state_change: Listener called with ``(breaker, old_state, new_state)``.
        name: Name for log messages, errors and events (defaults to the function's qualified name).
        logger: Logger (or name) for state transitions.

    Returns:
        The wrapped function, or a decorator when ``func`` is omitted.

    Raises:
        ValueError: If a numeric option is out of range.

    Example:
        >>> @circuit_breaker(failure_threshold=3, recovery_timeout=10)
        ... def call_api() -> None: ...
    """

    def decorate(f: Callable[..., Any]) -> Any:
        breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout=recovery_timeout,
            half_open_max_calls=half_open_max_calls,
            success_threshold=success_threshold,
            on=on,
            exclude=exclude,
            on_state_change=on_state_change,
            name=name or _core.qualname(f),
            logger=logger,
        )
        wrapped: Any = breaker(f)
        wrapped.breaker = breaker
        return wrapped

    return decorate if func is None else decorate(func)
