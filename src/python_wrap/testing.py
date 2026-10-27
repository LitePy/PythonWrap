"""Helpers for testing code that uses python-wrap (and code that waits or retries).

Everything here is standard library only. ``pytest`` is optional: the fixtures at the
bottom exist only when it can be imported, so this module imports fine without it.

Enable the fixtures with ``pytest_plugins = ["python_wrap.testing"]`` in a top-level
``conftest.py``; they are ``pw_fake_clock`` and ``pw_fast_mode``.

Nesting semantics of the override helpers
-----------------------------------------
Overrides are one process-wide state (tests use threads, so context variables would
hide them from worker threads). Each scope remembers the previous value of the fields
it changed and puts exactly those back on exit. Scopes nest correctly when they exit in
reverse order of entry, which ``with`` blocks and decorators always do. Scopes entered
from different threads that overlap and touch the same field are not isolated from each
other: the last one to exit wins. Use them from the test, not from concurrent workers.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading
import time
import types
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generic, ParamSpec, TypeVar, overload

from python_wrap import _core

try:
    import pytest as _pytest
except ImportError:  # pragma: no cover - exercised only without pytest installed
    _pytest = None  # type: ignore[assignment]

__all__ = [
    "Call",
    "FakeClock",
    "Spy",
    "disable_caching",
    "disable_rate_limits",
    "disable_retries",
    "eventually",
    "eventually_sync",
    "fake_clock",
    "fast_mode",
    "overrides",
    "spy",
    "spy_on",
]

P = ParamSpec("P")
R = TypeVar("R")
F = TypeVar("F", bound=Callable[..., Any])


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


_UNSET = _Unset()


# --------------------------------------------------------------------------- overrides


class _Scope:
    """Context manager and decorator that applies override fields and restores them."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values
        self._saved: list[dict[str, Any]] = []

    def __enter__(self) -> None:
        self._saved.append(_core.set_overrides(**self._values))

    def __exit__(self, *exc: object) -> None:
        _core.set_overrides(**self._saved.pop())

    def __call__(self, func: F) -> F:
        values = self._values
        if _core.is_async(func):

            @functools.wraps(func)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                with _Scope(values):
                    return await func(*args, **kwargs)

            return awrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with _Scope(values):
                return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]


def overrides(
    *,
    no_sleep: bool | _Unset = _UNSET,
    max_attempts: int | _Unset | None = _UNSET,
    disable_rate_limits: bool | _Unset = _UNSET,
    disable_caching: bool | _Unset = _UNSET,
) -> _Scope:
    """Temporarily change the process-wide test overrides.

    Use it as a context manager or as a decorator on sync and async functions. Only the
    fields you pass are changed; on exit exactly those are restored. See the module
    docstring for nesting semantics.

    Args:
        no_sleep: Make `_core.sleep` / `_core.async_sleep` return immediately.
        max_attempts: Cap `retry` at this many attempts (`None` removes the cap).
        disable_rate_limits: Let every call through rate limiters and throttles.
        disable_caching: Make caches always call the wrapped function.

    Raises:
        ValueError: If `max_attempts` is not `None` or an integer >= 1.

    Example:
        ```python
        from python_wrap import _core
        from python_wrap.testing import overrides

        with overrides(no_sleep=True):
            assert _core.overrides.no_sleep
        assert not _core.overrides.no_sleep
        ```
    """
    given = {
        "no_sleep": no_sleep,
        "max_attempts": max_attempts,
        "disable_rate_limits": disable_rate_limits,
        "disable_caching": disable_caching,
    }
    values = {k: v for k, v in given.items() if not isinstance(v, _Unset)}
    attempts = values.get("max_attempts")
    if attempts is not None and (not isinstance(attempts, int) or attempts < 1):
        # Validate eagerly so a bad value fails at the call, not on entry.
        raise ValueError("max_attempts must be None or an integer >= 1")
    return _Scope(values)


def fast_mode() -> _Scope:
    """Make every wait instant while keeping retries running.

    Sets `no_sleep=True` only: `retry` still retries the configured number of times and
    rate limiters still count, but nothing actually sleeps. Combine with
    `disable_retries()` if you also want a single attempt. Context manager and decorator.

    Example:
        ```python
        import time
        from python_wrap import retry
        from python_wrap.testing import fast_mode

        calls = []

        @retry(max_attempts=3, delay=60)
        def flaky() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionError
            return "ok"

        start = time.monotonic()
        with fast_mode():
            assert flaky() == "ok"
        assert time.monotonic() - start < 5
        ```
    """
    return _Scope({"no_sleep": True})


def disable_retries() -> _Scope:
    """Cap `retry` at one attempt (`max_attempts=1`). Context manager and decorator."""
    return _Scope({"max_attempts": 1})


def disable_rate_limits() -> _Scope:
    """Let every call through rate limiters and throttles. Context manager and decorator."""
    return _Scope({"disable_rate_limits": True})


def disable_caching() -> _Scope:
    """Make memoize/cache always call the wrapped function. Context manager and decorator."""
    return _Scope({"disable_caching": True})


# ------------------------------------------------------------------------------ clock


@dataclass
class FakeClock:
    """A controllable clock. Sleeping advances it instead of waiting.

    Attributes:
        start: The initial reading. Defaults to the real `time.monotonic()` at creation, so
            entries stamped before the fake clock was installed (a cache filled at import
            time, say) are in the fake clock's past and still expire as expected. Pass
            `start=` for a fixed reading.
        now: The current reading (starts at `start`).
        sleeps: Every duration passed to `sleep` / `async_sleep`, in order.

    Example:
        ```python
        from python_wrap.testing import FakeClock

        clock = FakeClock(start=10.0)
        clock.advance(5)
        clock.sleep(2)
        assert clock.now == 17.0
        assert clock.sleeps == [2]
        ```
    """

    start: float = field(default_factory=time.monotonic)
    now: float = field(init=False)
    sleeps: list[float] = field(default_factory=list)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        self.now = self.start

    @property
    def total_slept(self) -> float:
        """Sum of all recorded sleeps."""
        return sum(self.sleeps)

    def time(self) -> float:
        """The current reading (usable as `monotonic` / `perf_counter`)."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward without recording a sleep.

        Raises:
            ValueError: If `seconds` is negative.
        """
        if seconds < 0:
            raise ValueError("cannot advance a clock backwards")
        with self._lock:
            self.now += seconds

    def sleep(self, seconds: float) -> None:
        """Record a sleep and advance the clock by it (negative values advance nothing)."""
        with self._lock:
            self.sleeps.append(seconds)
            self.now += max(seconds, 0.0)

    async def async_sleep(self, seconds: float) -> None:
        """Like `sleep`, then yield to the event loop so other tasks can run."""
        self.sleep(seconds)
        await asyncio.sleep(0)


@contextmanager
def fake_clock(clock: FakeClock | None = None) -> Iterator[FakeClock]:
    """Patch `_core.monotonic/perf_counter/sleep/async_sleep` with a `FakeClock`.

    The previous time sources are restored on exit, so it nests.

    Args:
        clock: An existing clock to install; a fresh `FakeClock()` by default.

    Yields:
        The installed clock.

    Example:
        ```python
        from python_wrap import retry
        from python_wrap.testing import fake_clock

        calls = []

        @retry(max_attempts=3, delay=1.0, backoff=2, on=ValueError)
        def flaky() -> str:
            calls.append(1)
            if len(calls) < 3:
                raise ValueError
            return "ok"

        with fake_clock() as clock:
            assert flaky() == "ok"
        assert clock.sleeps == [1.0, 2.0]
        ```
    """
    clock = clock if clock is not None else FakeClock()
    names = ("monotonic", "perf_counter", "sleep", "async_sleep")
    with _core.overrides_lock:
        saved = {name: getattr(_core, name) for name in names}
        _core.monotonic = clock.time
        _core.perf_counter = clock.time
        _core.sleep = clock.sleep
        _core.async_sleep = clock.async_sleep
    try:
        yield clock
    finally:
        with _core.overrides_lock:
            for name, value in saved.items():
                setattr(_core, name, value)


# ------------------------------------------------------------------------------- spy


@dataclass(frozen=True)
class Call:
    """One recorded call: arguments, outcome and duration (via `_core.perf_counter`)."""

    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    result: Any = None
    exception: BaseException | None = None
    duration: float = 0.0


class Spy(Generic[P, R]):
    """A recording wrapper around a function. Create with `spy`.

    Works as a plain callable, as a decorator and on methods (bound access records
    `self` as the first argument). For `async def` functions the spy is awaitable.

    Attributes:
        calls: Recorded `Call` objects, oldest first.
    """

    def __init__(self, func: Callable[P, R]) -> None:
        self._func = func
        self.calls: list[Call] = []
        self._lock = threading.Lock()
        functools.update_wrapper(self, func)

    @property
    def call_count(self) -> int:
        """Number of recorded calls."""
        return len(self.calls)

    @property
    def called(self) -> bool:
        """True once the spy has been called."""
        return bool(self.calls)

    def reset(self) -> None:
        """Forget recorded calls."""
        with self._lock:
            self.calls.clear()

    def assert_called_with(self, *args: Any, **kwargs: Any) -> None:
        """Assert the most recent call used exactly these arguments.

        Raises:
            AssertionError: If never called or the last call differs.
        """
        if not self.calls:
            raise AssertionError("expected a call, but the spy was never called")
        last = self.calls[-1]
        if last.args != args or last.kwargs != kwargs:
            raise AssertionError(
                f"expected last call with args={args!r} kwargs={kwargs!r}, "
                f"got args={last.args!r} kwargs={last.kwargs!r}"
            )

    def _record(self, call: Call) -> None:
        with self._lock:
            self.calls.append(call)

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
        start = _core.perf_counter()
        try:
            result = self._func(*args, **kwargs)
        except BaseException as exc:
            self._record(Call(args, dict(kwargs), None, exc, _core.perf_counter() - start))
            raise
        self._record(Call(args, dict(kwargs), result, None, _core.perf_counter() - start))
        return result

    def __get__(self, obj: object, owner: type | None = None) -> Any:
        if obj is None:
            return self
        return types.MethodType(self, obj)


class _AsyncSpy(Spy[P, R]):
    async def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:  # type: ignore[override]
        start = _core.perf_counter()
        try:
            result: R = await self._func(*args, **kwargs)  # type: ignore[misc]
        except BaseException as exc:
            self._record(Call(args, dict(kwargs), None, exc, _core.perf_counter() - start))
            raise
        self._record(Call(args, dict(kwargs), result, None, _core.perf_counter() - start))
        return result


@overload
def spy(func: Callable[P, Awaitable[R]]) -> Spy[P, Awaitable[R]]: ...
@overload
def spy(func: Callable[P, R]) -> Spy[P, R]: ...
def spy(func: Callable[P, Any]) -> Spy[P, Any]:
    """Wrap `func` so every call is recorded, without changing its behaviour.

    Sync and async functions are supported (an `async def` spy records the awaited
    result when the coroutine finishes). Exceptions are recorded and re-raised.

    Args:
        func: The function to observe.

    Returns:
        A `Spy` with `.calls`, `.call_count`, `.called`, `.reset()` and
        `.assert_called_with(...)`.

    Example:
        ```python
        from python_wrap.testing import spy

        @spy
        def add(a: int, b: int) -> int:
            return a + b

        assert add(1, b=2) == 3
        assert add.call_count == 1
        add.assert_called_with(1, b=2)
        assert add.calls[0].result == 3
        ```
    """
    if _core.is_async(func):
        return _AsyncSpy(func)
    return Spy(func)


@contextmanager
def spy_on(obj: object, name: str) -> Iterator[Spy[..., Any]]:
    """Replace `obj.name` with a spy for the duration of the block, then restore it.

    Works on modules, classes (including static and class methods) and instances.

    Args:
        obj: The object holding the attribute.
        name: The attribute (function or method) name.

    Yields:
        The `Spy` now installed.

    Raises:
        AttributeError: If `obj` has no attribute `name`.

    Example:
        ```python
        import math
        from python_wrap.testing import spy_on

        with spy_on(math, "floor") as s:
            assert math.floor(1.5) == 1
        assert s.call_count == 1
        ```
    """
    raw = inspect.getattr_static(obj, name)
    had_own = name in getattr(obj, "__dict__", {})
    s = spy(getattr(obj, name))
    plain = isinstance(raw, (staticmethod, classmethod))
    setattr(obj, name, staticmethod(s) if plain else s)
    try:
        yield s
    finally:
        if had_own:
            setattr(obj, name, raw)
        else:
            delattr(obj, name)


# ------------------------------------------------------------------------- eventually


def _timeout_error(last: AssertionError, waited: float, attempts: int) -> AssertionError:
    return AssertionError(
        f"{last}\n(eventually: still failing after {waited:.3f}s and {attempts} attempt(s))"
    )


# The unpatched sleeps, so polling can tell "fast_mode without a fake clock" apart.
_REAL_SLEEP = _core.sleep
_REAL_ASYNC_SLEEP = _core.async_sleep


def _poll_sleep(interval: float) -> None:
    if _core.overrides.no_sleep and _core.sleep is _REAL_SLEEP:
        time.sleep(interval)  # fast_mode must not turn polling into a busy loop
    else:
        _core.sleep(interval)


async def _poll_async_sleep(interval: float) -> None:
    if _core.overrides.no_sleep and _core.async_sleep is _REAL_ASYNC_SLEEP:
        await asyncio.sleep(interval)
    else:
        await _core.async_sleep(interval)


async def eventually(
    assertion: Callable[[], object | Awaitable[object]],
    *,
    timeout: float = 1.0,
    interval: float = 0.01,
) -> None:
    """Retry `assertion` until it stops raising `AssertionError`, or time runs out.

    `assertion` may be sync or `async def`. Time is read through `_core.monotonic` and
    waited through `_core.async_sleep`, so it is real time by default (it is meant for
    integration tests) and follows `fake_clock()` if one is installed. Under
    `fast_mode()` (and no fake clock) it still pauses `interval` of real time between
    attempts, because the condition it waits for usually needs real time to pass.

    Args:
        assertion: Callable that raises `AssertionError` while the condition is false.
        timeout: Seconds to keep trying.
        interval: Seconds between attempts.

    Raises:
        AssertionError: The last failure, annotated with how long it waited. Other
            exceptions propagate immediately.

    Example:
        ```python
        import asyncio
        from python_wrap.testing import eventually

        state = {"ready": False}

        def check() -> None:
            assert state["ready"]

        async def main() -> None:
            asyncio.get_running_loop().call_later(0.02, state.update, {"ready": True})
            await eventually(check)

        asyncio.run(main())
        ```
    """
    start = _core.monotonic()
    attempts = 0
    while True:
        attempts += 1
        try:
            outcome = assertion()
            if inspect.isawaitable(outcome):
                await outcome
            return
        except AssertionError as exc:
            waited = _core.monotonic() - start
            if waited >= timeout:
                raise _timeout_error(exc, waited, attempts) from exc
        await _poll_async_sleep(interval)


def eventually_sync(
    assertion: Callable[[], object],
    *,
    timeout: float = 1.0,
    interval: float = 0.01,
) -> None:
    """Blocking version of `eventually` for plain sync tests.

    Args:
        assertion: Callable that raises `AssertionError` while the condition is false.
        timeout: Seconds to keep trying.
        interval: Seconds between attempts.

    Raises:
        AssertionError: The last failure, annotated with how long it waited.

    Example:
        ```python
        import threading
        from python_wrap.testing import eventually_sync

        done = threading.Event()
        threading.Timer(0.02, done.set).start()

        def check() -> None:
            assert done.is_set()

        eventually_sync(check)
        ```
    """
    start = _core.monotonic()
    attempts = 0
    while True:
        attempts += 1
        try:
            assertion()
            return
        except AssertionError as exc:
            waited = _core.monotonic() - start
            if waited >= timeout:
                raise _timeout_error(exc, waited, attempts) from exc
        _poll_sleep(interval)


# ------------------------------------------------------------------------ pytest plugin

if _pytest is not None:

    @_pytest.fixture
    def pw_fake_clock() -> Iterator[FakeClock]:
        """Install a `FakeClock` for the test and yield it."""
        with fake_clock() as clock:
            yield clock

    @_pytest.fixture
    def pw_fast_mode() -> Iterator[None]:
        """Run the test under `fast_mode()`."""
        with fast_mode():
            yield
