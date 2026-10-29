"""In-process call metrics: `count_calls` tracks calls, errors, concurrency and latency."""

from __future__ import annotations

import functools
import math
from collections import deque
from collections.abc import Callable
from typing import Any, Concatenate, ParamSpec, Protocol, TypeVar, cast, overload

from python_wrap import _core, _fork

__all__ = ["CallStats", "Counted", "count_calls"]

P = ParamSpec("P")
P2 = ParamSpec("P2")
R = TypeVar("R")
R_co = TypeVar("R_co", covariant=True)
T = TypeVar("T")


def _percentile(sorted_values: list[float], q: float) -> float:
    """Nearest-rank percentile of an already sorted list (0.0 when empty)."""
    if not sorted_values:
        return 0.0
    rank = max(math.ceil(q / 100 * len(sorted_values)), 1)
    return sorted_values[rank - 1]


class CallStats:
    """Live statistics of a function wrapped by `count_calls`.

    Every attribute is read under a lock at access time, so ``f.stats.calls`` always
    reflects the present. Latency figures (``mean``, ``p50``, ``p95``, ``p99``) cover the
    last ``window`` finished calls, successful or not, in seconds; they are ``0.0`` before
    the first call finishes. ``calls`` and ``errors`` count every call since the last
    reset, not just the window.

    Attributes:
        calls: Calls that have finished (returned or raised).
        errors: Calls that raised an exception.
        in_flight: Calls running right now.
        last_duration: Seconds the most recently finished call took.
        mean: Mean duration over the window.
        p50: Median duration over the window (nearest rank).
        p95: 95th percentile duration over the window.
        p99: 99th percentile duration over the window.
    """

    def __init__(self, window: int) -> None:
        self._window = window
        self._lock = _fork.ForkSafeLock()
        self._durations: deque[float] = deque(maxlen=window)
        self._calls = 0
        self._errors = 0
        self._in_flight = 0
        self._last = 0.0
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._in_flight = 0

    def _begin(self) -> None:
        with self._lock:
            self._in_flight += 1

    def _end(self, duration: float, ok: bool) -> None:
        with self._lock:
            self._in_flight -= 1
            self._calls += 1
            if not ok:
                self._errors += 1
            self._last = duration
            self._durations.append(duration)

    def _reset(self) -> None:
        with self._lock:
            self._durations.clear()
            self._calls = self._errors = 0
            self._last = 0.0
            # in_flight is left alone: those calls are still running and will finish.

    def _sorted(self) -> list[float]:
        with self._lock:
            return sorted(self._durations)

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    @property
    def errors(self) -> int:
        with self._lock:
            return self._errors

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    @property
    def last_duration(self) -> float:
        with self._lock:
            return self._last

    @property
    def mean(self) -> float:
        values = self._sorted()
        return sum(values) / len(values) if values else 0.0

    @property
    def p50(self) -> float:
        return _percentile(self._sorted(), 50)

    @property
    def p95(self) -> float:
        return _percentile(self._sorted(), 95)

    @property
    def p99(self) -> float:
        return _percentile(self._sorted(), 99)

    def __repr__(self) -> str:
        return (
            f"CallStats(calls={self.calls}, errors={self.errors}, in_flight={self.in_flight}, "
            f"mean={self.mean:.6f}, p95={self.p95:.6f})"
        )


class Counted(Protocol[P, R_co]):
    """A function wrapped by `count_calls` (callable plus ``stats`` and ``reset()``).

    Use it to annotate decorated functions, e.g. ``Counted[[int], str]``. The ``__get__``
    overloads let type checkers bind ``self`` on instance access and ``cls`` under a
    ``@classmethod`` stacked above, with the same typing limitations as `Memoized`
    (see `python_wrap.caching`). The statistics belong to the function, so they are
    shared by every instance of a class.
    """

    stats: CallStats

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...

    @overload
    def __get__(
        self: Counted[Concatenate[type[T], P2], R_co], instance: None, owner: type[T]
    ) -> _CountedOnClass[P, P2, R_co]: ...
    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Counted[P, R_co]: ...
    @overload
    def __get__(
        self: Counted[Concatenate[Any, P2], R_co], instance: object, owner: type | None = None
    ) -> Counted[P2, R_co]: ...

    def reset(self) -> None:
        """Zero the counters and clear the duration window."""


class _CountedOnClass(Counted[P, R_co], Protocol[P, P2, R_co]):
    """`Counted` read from a class: accepts the call with and without the first argument."""

    @overload
    def __call__(self, *args: P2.args, **kwargs: P2.kwargs) -> R_co: ...
    @overload
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...


@overload
def count_calls(func: Callable[P, R], /) -> Counted[P, R]: ...
@overload
def count_calls(
    func: None = None, /, *, window: int = 1000
) -> Callable[[Callable[P, R]], Counted[P, R]]: ...
def count_calls(func: Callable[..., Any] | None = None, /, *, window: int = 1000) -> Any:
    """Track how often a function runs, fails, and how long it takes.

    The wrapper exposes ``.stats`` (a live `CallStats`: ``calls``, ``errors``,
    ``in_flight``, ``last_duration``, ``mean``, ``p50``, ``p95``, ``p99``) and
    ``.reset()``. Sync and ``async def`` functions are supported (async calls are timed
    across the whole awaited coroutine), as are methods; statistics are per function, not
    per instance. A call that raises counts as an error and its exception propagates.
    Cancellation counts as an error too. Updates are thread-safe.

    Args:
        func: The function, when used as a bare ``@count_calls``.
        window: How many of the most recent durations feed ``mean`` and the percentiles.

    Returns:
        The wrapped function, a `Counted`.

    Raises:
        ValueError: If ``window`` is less than 1.

    Example:
        >>> @count_calls(window=500)
        ... def work() -> int:
        ...     return 1
        >>> work()
        1
        >>> work.stats.calls
        1
    """
    if window < 1:
        raise ValueError("window must be >= 1")

    def decorate(f: Callable[P, R]) -> Counted[P, R]:
        stats = CallStats(window)

        if _core.is_async(f):

            @functools.wraps(f)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                stats._begin()
                began = _core.perf_counter()
                ok = False
                try:
                    result = await f(*args, **kwargs)  # type: ignore[misc]
                    ok = True
                    return result
                finally:
                    stats._end(_core.perf_counter() - began, ok)

            wrapper: Any = awrapper
        else:

            @functools.wraps(f)
            def swrapper(*args: Any, **kwargs: Any) -> Any:
                stats._begin()
                began = _core.perf_counter()
                ok = False
                try:
                    result = f(*args, **kwargs)
                    ok = True
                    return result
                finally:
                    stats._end(_core.perf_counter() - began, ok)

            wrapper = swrapper

        wrapper.stats = stats
        wrapper.reset = stats._reset
        return cast("Counted[P, R]", wrapper)

    return decorate if func is None else decorate(func)
