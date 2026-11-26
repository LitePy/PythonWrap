"""`throttle` and `debounce`: run at most once per interval, or once activity stops."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import threading
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Hashable
from typing import (
    Any,
    Concatenate,
    ParamSpec,
    Protocol,
    TypeVar,
    cast,
    overload,
)

from python_wrap import _core, _fork
from python_wrap._buckets import _Buckets
from python_wrap._core import P

T = TypeVar("T")
R_co = TypeVar("R_co", covariant=True)
P2 = ParamSpec("P2")
S = TypeVar("S")


class _ThrottleDecorator(Protocol):
    @overload
    def __call__(
        self, func: Callable[P, Coroutine[Any, Any, T]], /
    ) -> Callable[P, Coroutine[Any, Any, T | None]]: ...
    @overload
    def __call__(self, func: Callable[P, T], /) -> Callable[P, T | None]: ...


class _ThrottleState:
    """One throttle window: when the last run started and what the last run returned."""

    __slots__ = ("last_result", "last_start")

    def __init__(self) -> None:
        self.last_start: float | None = None
        self.last_result: Any = None


def throttle(
    interval: float,
    *,
    key: Callable[..., Hashable] | None = None,
    per_instance: bool = False,
) -> _ThrottleDecorator:
    """Run the function at most once per ``interval`` seconds.

    The first call runs and opens a window of ``interval`` seconds (measured from when
    the call started). Calls inside the window do not run; they return the result of the
    most recent run that *returned* (``None`` if none has returned yet). A call that
    raises propagates its exception to its own caller and still uses up its window, but
    does not replace the remembered result: calls skipped inside that window get the
    previous successful result (or ``None``), not the error. Thread-safe; the function
    itself runs outside the lock, so a slow call does not block skipped callers.
    ``async def`` functions get the same semantics with an ``async`` wrapper.

    The window belongs to the decorated function, so on a method every instance shares
    it. ``key`` gives each key its own window and ``per_instance`` gives each instance
    its own; they combine (a window per instance and key).

    Args:
        interval: Minimum seconds between runs; must be positive.
        key: Optional function called with the call's arguments (including ``self`` for
            methods) that returns a hashable key, e.g. a user id; each key has its own
            window, like ``key=`` on `rate_limit`. Windows that have run out are dropped
            now and then, so keys do not pile up.
        per_instance: For methods: one window per instance, found by identity through a
            weak reference, so instances stay collectable and need not be hashable. They
            must be weak-referenceable (a ``__slots__`` class must list ``'__weakref__'``).

    Returns:
        A decorator; the wrapped function returns ``R | None``.

    Raises:
        ValueError: If ``interval`` is not positive.
        TypeError: At call time with ``per_instance``, for an instance that cannot be
            weakly referenced.

    Example:
        >>> @throttle(1.0)
        ... def refresh() -> str:
        ...     return "fresh"
        >>> class Sensor:
        ...     @throttle(1.0, per_instance=True)
        ...     def read(self) -> str: ...
    """
    if interval <= 0:
        raise ValueError(f"throttle interval must be positive, got {interval!r}")

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        lock = _fork.ForkSafeLock()
        buckets = _Buckets(
            _ThrottleState,
            key=key,
            per_instance=per_instance,
            idle=lambda st: st.last_start is None or _core.monotonic() - st.last_start >= interval,
            what="throttle",
        )

        name = _core.qualname(func)

        def should_run(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[Any, bool, Any]:
            state = buckets.get(args, kwargs)
            if _core.overrides.disable_rate_limits:
                return state, True, None
            now = _core.monotonic()
            with lock:
                if state.last_start is None or now - state.last_start >= interval:
                    state.last_start = now
                    return state, True, None
                previous = state.last_result
            _core.emit("throttle.skipped", name)
            return state, False, previous

        def store(state: _ThrottleState, result: Any) -> Any:
            with lock:
                state.last_result = result
            return result

        if _core.is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                state, run, previous = should_run(args, kwargs)
                if not run:
                    return previous
                return store(state, await func(*args, **kwargs))

            return async_wrapper

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            state, run, previous = should_run(args, kwargs)
            if not run:
                return previous
            return store(state, func(*args, **kwargs))

        return wrapper

    return cast(_ThrottleDecorator, decorate)


# ---------------------------------------------------------------------------------------
# debounce
# ---------------------------------------------------------------------------------------


class Debounced(Protocol[P, R_co]):
    """A debounced sync function: calls return ``None``; the real call happens later.

    Binds ``self`` and ``cls`` like `python_wrap.caching.Memoized`, with the same typing
    limitations.
    """

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> None: ...

    @overload
    def __get__(
        self: Debounced[Concatenate[type[T], P2], R_co], instance: None, owner: type[T]
    ) -> _DebouncedOnClass[P, P2, R_co]: ...
    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Debounced[P, R_co]: ...
    @overload
    def __get__(
        self: Debounced[Concatenate[Any, P2], R_co], instance: object, owner: type | None = None
    ) -> Debounced[P2, R_co]: ...

    def cancel(self, *args: Any, **kwargs: Any) -> None:
        """Drop the pending call, if any (``key``/``per_instance``: pass a call's arguments)."""

    def flush(self, *args: Any, **kwargs: Any) -> R_co | None:
        """Run the pending call now and return its result (``None`` if nothing pending)."""


class _DebouncedOnClass(Debounced[P, R_co], Protocol[P, P2, R_co]):
    """`Debounced` read from a class: accepts the call with and without the first argument.

    Returned for class access (``Cls.attr``) when the first parameter could be ``cls``.
    pyright only picks it under ``@classmethod``; mypy cannot tell a classmethod from an
    instance or static method there, so both call forms are accepted.
    """

    @overload
    def __call__(self, *args: P2.args, **kwargs: P2.kwargs) -> None: ...
    @overload
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> None: ...


class AsyncDebounced(Protocol[P, R_co]):
    """A debounced ``async def`` function; see `debounce` for the await semantics.

    Binds ``self`` and ``cls`` like `python_wrap.caching.Memoized`, with the same typing
    limitations.
    """

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Coroutine[Any, Any, R_co]: ...

    @overload
    def __get__(
        self: AsyncDebounced[Concatenate[type[T], P2], R_co], instance: None, owner: type[T]
    ) -> _AsyncDebouncedOnClass[P, P2, R_co]: ...
    @overload
    def __get__(self, instance: None, owner: type | None = None) -> AsyncDebounced[P, R_co]: ...
    @overload
    def __get__(
        self: AsyncDebounced[Concatenate[Any, P2], R_co],
        instance: object,
        owner: type | None = None,
    ) -> AsyncDebounced[P2, R_co]: ...

    def cancel(self, *args: Any, **kwargs: Any) -> None:
        """Drop the pending call; callers awaiting it get ``CancelledError``."""

    def flush(self, *args: Any, **kwargs: Any) -> Coroutine[Any, Any, R_co | None]:
        """Run the pending call now; awaiting returns its result (``None`` if none)."""


class _AsyncDebouncedOnClass(AsyncDebounced[P, R_co], Protocol[P, P2, R_co]):
    """`AsyncDebounced` read from a class: accepts the call with and without the first argument.

    Returned for class access (``Cls.attr``) when the first parameter could be ``cls``.
    pyright only picks it under ``@classmethod``; mypy cannot tell a classmethod from an
    instance or static method there, so both call forms are accepted.
    """

    @overload
    def __call__(self, *args: P2.args, **kwargs: P2.kwargs) -> Coroutine[Any, Any, R_co]: ...
    @overload
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Coroutine[Any, Any, R_co]: ...


class _DebounceDecorator(Protocol):
    @overload
    def __call__(  # type: ignore[overload-overlap]
        self, func: Callable[P, Coroutine[Any, Any, T]], /
    ) -> AsyncDebounced[P, T]: ...
    @overload
    def __call__(self, func: Callable[P, T], /) -> Debounced[P, T]: ...


_Pending = tuple[tuple[Any, ...], dict[str, Any]]


class _SyncDebState:
    """One sync debounce slot: the running timer and the call waiting for it."""

    __slots__ = ("pending", "timer")

    def __init__(self) -> None:
        self.timer: threading.Timer | None = None
        self.pending: _Pending | None = None


def _debounce_sync(
    func: Callable[..., Any],
    wait: float,
    key: Callable[..., Hashable] | None,
    per_instance: bool,
) -> Callable[..., None]:
    lock = _fork.ForkSafeLock()
    buckets = _Buckets(
        _SyncDebState,
        key=key,
        per_instance=per_instance,
        idle=lambda st: st.timer is None and st.pending is None,
        what="debounce",
    )

    def take(st: _SyncDebState) -> _Pending | None:
        if st.timer is not None:
            st.timer.cancel()
        call, st.timer, st.pending = st.pending, None, None
        return call

    def fire(st: _SyncDebState, me: list[threading.Timer]) -> None:
        with lock:
            if st.timer is not me[0]:  # superseded or cancelled meanwhile
                return
            call = take(st)
        if call is not None:
            func(*call[0], **call[1])

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        st = buckets.get(args, kwargs)
        with lock:
            take(st)
            me: list[threading.Timer] = []
            timer = st.timer = threading.Timer(wait, fire, args=(st, me))
            me.append(timer)
            timer.daemon = True
            st.pending = (args, kwargs)
            timer.start()

    def selected(args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[_SyncDebState] | None:
        """The states to act on, or None if the arguments name a state that does not exist."""
        if buckets.selective and (args or kwargs):
            found = buckets.find(args, kwargs)
            return None if found is None else [found]
        return buckets.states()

    def cancel(*args: Any, **kwargs: Any) -> None:
        with lock:
            for st in selected(args, kwargs) or ():
                take(st)

    def flush(*args: Any, **kwargs: Any) -> Any:
        with lock:
            calls = [take(st) for st in selected(args, kwargs) or ()]
        results = [func(*call[0], **call[1]) for call in calls if call is not None]
        return results[0] if len(results) == 1 else None

    wrapper.cancel = cancel  # type: ignore[attr-defined]
    wrapper.flush = flush  # type: ignore[attr-defined]
    return wrapper


class _AsyncBurst:
    """One burst of async debounced calls: the latest arguments and a shared outcome."""

    def __init__(self, loop: asyncio.AbstractEventLoop, state: _AsyncDebState) -> None:
        self.outcome: asyncio.Future[Any] = loop.create_future()
        self.timer: asyncio.Task[None] | None = None
        self.call: _Pending | None = None
        self.state = state


class _AsyncDebState:
    """One async debounce slot: its pending burst per event loop."""

    __slots__ = ("bursts",)

    def __init__(self) -> None:
        self.bursts: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _AsyncBurst] = (
            weakref.WeakKeyDictionary()
        )


def _debounce_async(
    func: Callable[..., Awaitable[Any]],
    wait: float,
    key: Callable[..., Hashable] | None,
    per_instance: bool,
) -> Callable[..., Any]:
    # Several loops (in several threads) share each state's `bursts`; each burst itself
    # is only ever touched from its own loop's thread.
    bursts_lock = _fork.ForkSafeLock()
    buckets = _Buckets(
        _AsyncDebState,
        key=key,
        per_instance=per_instance,
        idle=lambda st: not st.bursts,
        what="debounce",
    )

    async def run(burst: _AsyncBurst) -> Any:
        loop = asyncio.get_running_loop()
        with bursts_lock:
            if burst.state.bursts.get(loop) is burst:
                del burst.state.bursts[loop]
        call = burst.call
        if call is None or burst.outcome.done():
            return None
        try:
            result = await func(*call[0], **call[1])
        except asyncio.CancelledError:
            burst.outcome.cancel()
            raise
        except BaseException as exc:
            if not burst.outcome.done():
                burst.outcome.set_exception(exc)
                burst.outcome.exception()  # mark retrieved: callers may all be gone
            raise
        if not burst.outcome.done():
            burst.outcome.set_result(result)
        return result

    async def sleep_then_run(burst: _AsyncBurst) -> None:
        await asyncio.sleep(wait)  # real time, like the sync `threading.Timer`
        burst.timer = None  # past the cancellation point: run even if called again now
        with contextlib.suppress(Exception):  # delivered to callers via burst.outcome
            await run(burst)

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        st = buckets.get(args, kwargs)
        with bursts_lock:
            burst = st.bursts.get(loop)
            if burst is None:
                burst = st.bursts[loop] = _AsyncBurst(loop, st)
        if burst.timer is not None:
            burst.timer.cancel()
        burst.call = (args, kwargs)
        burst.timer = loop.create_task(sleep_then_run(burst))
        return await asyncio.shield(burst.outcome)

    def cancel_burst(burst: _AsyncBurst) -> None:
        if burst.timer is not None:
            burst.timer.cancel()
        burst.outcome.cancel()

    def selected(args: tuple[Any, ...], kwargs: dict[str, Any]) -> list[_AsyncDebState]:
        if buckets.selective and (args or kwargs):
            found = buckets.find(args, kwargs)
            return [] if found is None else [found]
        return buckets.states()

    def cancel(*args: Any, **kwargs: Any) -> None:
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        taken: list[tuple[asyncio.AbstractEventLoop, _AsyncBurst | None]] = []
        states = selected(args, kwargs)
        with bursts_lock:
            for st in states:
                if running is not None:
                    taken.append((running, st.bursts.pop(running, None)))
                else:
                    taken.extend((lp, st.bursts.pop(lp)) for lp in list(st.bursts))
        for lp, burst in taken:
            if burst is None:
                continue
            if lp.is_closed():
                continue
            if lp is running or not lp.is_running():
                cancel_burst(burst)
            else:
                # Another thread's loop: futures are not thread-safe, so hand the
                # cancellation to that loop (which also wakes it up).
                with contextlib.suppress(RuntimeError):  # closed meanwhile
                    lp.call_soon_threadsafe(cancel_burst, burst)

    async def flush(*args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        states = selected(args, kwargs)
        with bursts_lock:
            found = [b for b in (st.bursts.get(loop) for st in states) if b is not None]
        results = []
        for burst in found:
            if burst.timer is not None:
                burst.timer.cancel()
                burst.timer = None
            results.append(await run(burst))
        return results[0] if len(results) == 1 else None

    wrapper.cancel = cancel  # type: ignore[attr-defined]
    wrapper.flush = flush  # type: ignore[attr-defined]
    return wrapper


def debounce(
    wait: float,
    *,
    key: Callable[..., Hashable] | None = None,
    per_instance: bool = False,
) -> _DebounceDecorator:
    """Postpone running until ``wait`` seconds have passed since the last call.

    Each call restarts the countdown and replaces the pending arguments, so only the last
    call of a burst runs.

    * Sync functions: the countdown is a daemon `threading.Timer`, so the function
      runs in the timer's thread. The wrapper returns ``None`` immediately.
    * ``async def`` functions: a call schedules a task on the running loop (cancelling the
      previously scheduled one) and then waits for the burst to finish. **Every caller in
      a burst awaits the same outcome** — the result (or exception) of the final call —
      so superseded callers are not left hanging or cancelled. Calls must be awaited (or
      wrapped in a task) to take effect; a single task awaiting sequentially simply waits
      ``wait`` seconds per call. Bursts are tracked per event loop.

    The wrapper also has:

    * ``.cancel()`` — drop the pending call (async awaiters get ``CancelledError``).
      Thread-safe. For ``async def`` functions, inside a running loop it cancels that
      loop's pending call; from a thread with no running loop it cancels the pending
      call of every loop (handing the cancellation to each loop's own thread).
    * ``.flush()`` — run the pending call right now and return its result, or ``None``
      if nothing is pending. For ``async def`` functions it is a coroutine to ``await``.

    State belongs to the decorated function, so on a method it is shared by all
    instances: calls on different instances debounce each other. ``key`` gives each key
    its own countdown and pending call, and ``per_instance`` gives each instance its own.
    Then ``.cancel()`` and ``.flush()`` take the arguments of a call to say which one
    (``obj.save.cancel(obj)``, since the attribute of a bound method is the function's);
    without arguments they act on every pending call, and ``.flush()`` returns the result
    only when exactly one call was flushed.

    Args:
        wait: Quiet period in seconds; must be non-negative.
        key: Optional function called with the call's arguments (including ``self`` for
            methods) that returns a hashable key; calls with different keys do not
            debounce each other. Idle keys are dropped now and then.
        per_instance: For methods: one debounce slot per instance, found by identity
            through a weak reference, so instances stay collectable and need not be
            hashable. They must be weak-referenceable (a ``__slots__`` class must list
            ``'__weakref__'``). A pending call keeps its instance alive until it runs.

    Returns:
        A decorator producing a `Debounced` or `AsyncDebounced` wrapper.

    Raises:
        ValueError: If ``wait`` is negative.
        TypeError: At call time with ``per_instance``, for an instance that cannot be
            weakly referenced.

    Example:
        >>> @debounce(0.3)
        ... def save(text: str) -> None: ...
        >>> save("a"); save("ab"); save.flush()  # runs once, with "ab"
        >>> @debounce(0.3, key=lambda doc_id, *_: doc_id)
        ... def autosave(doc_id: int, text: str) -> None: ...
    """
    if wait < 0:
        raise ValueError(f"debounce wait must be non-negative, got {wait!r}")

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        if _core.is_async(func):
            return _debounce_async(func, wait, key, per_instance)
        return _debounce_sync(func, wait, key, per_instance)

    return cast(_DebounceDecorator, decorate)


# ---------------------------------------------------------------------------------------
# singleflight
# ---------------------------------------------------------------------------------------
