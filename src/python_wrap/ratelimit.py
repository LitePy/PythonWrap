"""`rate_limit`, `RateLimiter` and `limiter_of`: sliding-window call limits."""

from __future__ import annotations

import asyncio
import functools
from collections import deque
from collections.abc import Callable, Hashable, Iterable
from types import TracebackType
from typing import (
    Any,
    NoReturn,
    ParamSpec,
    TypeVar,
    cast,
)

from python_wrap import _core, _fork
from python_wrap._buckets import _Buckets
from python_wrap._core import F
from python_wrap.errors import RateLimitExceeded

T = TypeVar("T")
R_co = TypeVar("R_co", covariant=True)
P2 = ParamSpec("P2")
S = TypeVar("S")

_SWEEP_EVERY = 1024

Limit = tuple[int, float]
"""One rate limit: ``(calls, period_seconds)``."""


def _limits(
    calls: int | None, period: float | None, limits: Iterable[Limit] | None
) -> tuple[Limit, ...]:
    """Validate and combine the ``calls``/``period`` pair and ``limits``."""
    result: list[Limit] = []
    if calls is not None or period is not None:
        if calls is None or period is None:
            raise TypeError("rate limit: give both calls and period")
        result.append((calls, period))
    if limits is not None:
        result.extend((c, p) for c, p in limits)
    if not result:
        raise TypeError("rate limit: give calls and period, or limits=[(calls, period), ...]")
    for c, p in result:
        if c < 1:
            raise ValueError(f"rate_limit calls must be >= 1, got {c!r}")
        if p <= 0:
            raise ValueError(f"rate_limit period must be positive, got {p!r}")
    return tuple(result)


def _describe(limits: tuple[Limit, ...]) -> str:
    return " and ".join(f"{c} calls per {p:g}s" for c, p in limits)


class _SlidingWindow:
    """Thread-safe sliding-window logs of call timestamps: per key, one deque per limit."""

    def __init__(self, limits: tuple[Limit, ...]) -> None:
        self.limits = limits
        self._lock = _fork.ForkSafeLock()
        self._buckets: dict[Hashable, tuple[deque[float], ...]] = {}
        self._since_sweep = 0
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._buckets.clear()
        self._since_sweep = 0

    def acquire(self, key: Hashable) -> float:
        """Record a call and return 0.0, or return how long to wait before retrying.

        A call is recorded only if *every* limit allows it.
        """
        with self._lock:
            # Read the clock under the lock: a call that queued for the lock must be
            # recorded at the time it is admitted, not the time it arrived.
            now = _core.monotonic()
            self._since_sweep += 1
            if self._since_sweep >= _SWEEP_EVERY:
                self._sweep(now)
            buckets = self._buckets.get(key)
            if buckets is None:
                buckets = self._buckets[key] = tuple(deque() for _ in self.limits)
            wait = 0.0
            for (calls, period), bucket in zip(self.limits, buckets, strict=True):
                horizon = now - period
                while bucket and bucket[0] <= horizon:
                    bucket.popleft()
                if len(bucket) >= calls:
                    wait = max(wait, bucket[0] + period - now)
            if wait > 0:
                return wait
            for bucket in buckets:
                bucket.append(now)
            return 0.0

    def admit(self, key: Hashable) -> None:
        """Record a call now even if a limit is full (for `fast_mode()`'s blocked calls)."""
        with self._lock:
            now = _core.monotonic()
            buckets = self._buckets.get(key)
            if buckets is None:
                buckets = self._buckets[key] = tuple(deque() for _ in self.limits)
            for bucket in buckets:
                bucket.append(now)

    def clear(self) -> None:
        with self._lock:
            self._buckets.clear()
            self._since_sweep = 0

    def _sweep(self, now: float) -> None:
        self._since_sweep = 0
        periods = [period for _, period in self.limits]

        def is_stale(buckets: tuple[deque[float], ...]) -> bool:
            return all(not b or b[-1] <= now - p for p, b in zip(periods, buckets, strict=True))

        stale = [k for k, buckets in self._buckets.items() if is_stale(buckets)]
        for k in stale:
            del self._buckets[k]


class RateLimiter:
    """A reusable sliding-window rate limit, shared by everything that uses it.

    Use one instance to give several functions or code blocks a single budget:

    * as a decorator (on sync or ``async def`` functions and methods);
    * as ``with limiter:`` / ``async with limiter:`` around a block;
    * through ``acquire()`` / ``try_acquire()`` / ``acquire_async()``.

    A call is allowed when fewer than ``calls`` calls started in the last ``period``
    seconds (a sliding-window log, so no ``2 * calls`` burst at a window boundary). With
    ``limits=[(5, 1.0), (600, 900.0)]`` every limit must allow the call. Rejected or
    still-waiting calls are not counted. Thread-safe; async callers wait with ``await``
    and never block the event loop. While ``python_wrap.testing`` disables rate limits,
    every call goes through and nothing is recorded. Under ``fast_mode()`` (``no_sleep``)
    a blocking call that would wait is let through at once and still recorded, so later
    non-blocking calls see the limit; under ``fake_clock()`` the wait advances the fake
    clock instead.

    Events: ``rate_limit.rejected(retry_after)`` for each rejected call and
    ``rate_limit.waited(seconds)`` after a call had to wait, reported under ``name``
    (or the decorated function's qualified name).

    Args:
        calls: Maximum number of calls per ``period`` (>= 1).
        period: Window length in seconds (> 0).
        limits: More ``(calls, period)`` pairs; may replace or add to ``calls``/``period``.
        block: What the decorator and ``with`` do when over the limit: ``False``
            (default) raises `RateLimitExceeded`, ``True`` waits for a slot.
        name: Label for error messages and events.
        backend: A shared window such as `python_wrap.quotas.RedisWindow` (called in a
            worker thread from async code) or `python_wrap.quotas.AsyncRedisWindow`
            (awaited on the event loop; such a limiter only works in ``async def`` code).

    Raises:
        TypeError: If no limit is given, or only one of ``calls`` and ``period``.
        ValueError: If a limit is out of range.

    Example:
        ```python
        from python_wrap.concurrency import RateLimiter

        github = RateLimiter(limits=[(4, 1.0), (5000, 3600.0)], name="github")

        @github
        def get_repo(name: str) -> str:
            return name

        @github
        def get_user(name: str) -> str:
            return name

        assert get_repo("a") == "a" and get_user("b") == "b"  # one shared budget
        with github:  # a plain block counts too
            pass
        assert github.try_acquire()
        assert not github.try_acquire()  # 4 calls in the last second
        ```
    """

    def __reduce__(self) -> NoReturn:
        _core.refuse_pickle(self)

    def __init__(
        self,
        calls: int | None = None,
        period: float | None = None,
        *,
        limits: Iterable[Limit] | None = None,
        block: bool = False,
        name: str | None = None,
        backend: Any = None,
    ) -> None:
        if backend is not None and calls is None and limits is None:
            calls, period = backend.calls, backend.period
        self._backend = backend
        # A backend whose acquire() is a coroutine (redis.asyncio) is awaited directly.
        self._async_backend = backend is not None and _core.is_async(backend.acquire)
        self.limits = _limits(calls, period, limits)
        if backend is not None and self.limits != ((backend.calls, backend.period),):
            raise ValueError("limiter limits must match its backend's calls and period")
        self.block = block
        self.name = name
        self._window: Any = backend if backend is not None else _SlidingWindow(self.limits)

    def __repr__(self) -> str:
        return f"RateLimiter(name={self.name!r}, limits={list(self.limits)!r})"

    # -- core ---------------------------------------------------------------------------

    def _rejected(self, label: str, retry_after: float) -> RateLimitExceeded:
        _core.emit("rate_limit.rejected", label, retry_after=retry_after)
        return RateLimitExceeded(
            f"{label} rate limit of {_describe(self.limits)} exceeded; "
            f"retry after {retry_after:.3g}s",
            retry_after=retry_after,
        )

    def _gives_up(self, label: str, wait: float, give_up: float | None) -> bool:
        """True (after emitting the rejection) if ``wait`` overshoots ``give_up``."""
        if give_up is not None and _core.monotonic() + wait > give_up:
            _core.emit("rate_limit.rejected", label, retry_after=wait)
            return True
        return False

    def _take(
        self, key: Hashable, label: str, *, block: bool, timeout: float | None, strict: bool
    ) -> bool:
        """Take a slot in a thread. ``strict`` raises on rejection instead of returning False."""
        if _core.overrides.disable_rate_limits:
            return True
        block = block and not _core.no_block.get()
        if self._async_backend:
            raise TypeError(
                f"{label}: this limiter has an async backend; use it from async def code "
                "(acquire_async, async with, or an async decorated function)"
            )
        wait = self._window.acquire(key)
        if wait <= 0:
            return True
        if not block:
            error = self._rejected(label, wait)
            if strict:
                raise error
            return False
        start = _core.monotonic()
        give_up = None if timeout is None else start + timeout
        while wait > 0:
            if self._gives_up(label, wait, give_up):
                return False
            _core.sleep(wait)
            if _core.overrides.disable_rate_limits:
                break
            wait = self._window.acquire(key)
            if wait > 0 and _core.overrides.no_sleep:
                # fast_mode() and the sleep did not move the clock: admit the call
                # instead of spinning (it still counts toward the limit).
                self._window.admit(key)
                break
        _core.emit("rate_limit.waited", label, seconds=_core.monotonic() - start)
        return True

    async def _aacquire(self, key: Hashable) -> float:
        if self._async_backend:
            return float(await self._window.acquire(key))
        if self._backend is not None:
            return float(await asyncio.to_thread(self._window.acquire, key))
        return float(self._window.acquire(key))

    async def _atake(
        self, key: Hashable, label: str, *, block: bool, timeout: float | None, strict: bool
    ) -> bool:
        """`_take` for coroutines: waits with ``await``."""
        if _core.overrides.disable_rate_limits:
            return True
        block = block and not _core.no_block.get()
        wait = await self._aacquire(key)
        if wait <= 0:
            return True
        if not block:
            error = self._rejected(label, wait)
            if strict:
                raise error
            return False
        start = _core.monotonic()
        give_up = None if timeout is None else start + timeout
        while wait > 0:
            if self._gives_up(label, wait, give_up):
                return False
            await _core.async_sleep(wait)
            if _core.overrides.disable_rate_limits:
                break
            wait = await self._aacquire(key)
            if wait > 0 and _core.overrides.no_sleep:
                if self._backend is not None and not self._async_backend:
                    await asyncio.to_thread(self._window.admit, key)
                else:
                    self._window.admit(key)
                break
        _core.emit("rate_limit.waited", label, seconds=_core.monotonic() - start)
        return True

    def _label(self) -> str:
        return self.name or "rate_limiter"

    # -- explicit API -------------------------------------------------------------------

    def reset(self) -> None:
        """Forget every recorded call (for every key), as if the limiter were new.

        Meant for tests: share one `RateLimiter` (or reach a `rate_limit` limiter with
        `limiter_of`) and reset it between tests. Thread-safe; calls waiting for a slot
        re-check and may then go through. With an async backend, ``await
        backend.clear()`` instead.
        """
        if self._async_backend:
            raise TypeError("this limiter has an async backend; await backend.clear() instead")
        self._window.clear()

    def try_acquire(self) -> bool:
        """Take a slot if one is free right now.

        Returns:
            ``True`` if the call may proceed (it is counted), ``False`` otherwise.
        """
        return self._take(None, self._label(), block=False, timeout=None, strict=False)

    def acquire(self, block: bool = True, timeout: float | None = None) -> bool:
        """Take a slot, waiting for one in this thread if ``block`` is true.

        Never call it from a coroutine; use `acquire_async` there.

        Args:
            block: Wait for a slot (``True``) or return at once (``False``).
            timeout: Give up after this many seconds (``None`` waits as long as needed).
                Slots only free up as time passes, so the call returns ``False`` right
                away when the next slot is further away than ``timeout``.

        Returns:
            ``True`` if a slot was taken, ``False`` if not.
        """
        return self._take(None, self._label(), block=block, timeout=timeout, strict=False)

    async def acquire_async(self, block: bool = True, timeout: float | None = None) -> bool:
        """`acquire` for async code: waits with ``await``, never blocking the loop.

        Args:
            block: Wait for a slot (``True``) or return at once (``False``).
            timeout: Give up after this many seconds (``None`` waits as long as needed).

        Returns:
            ``True`` if a slot was taken, ``False`` if not.
        """
        return await self._atake(None, self._label(), block=block, timeout=timeout, strict=False)

    def __enter__(self) -> RateLimiter:
        self._take(None, self._label(), block=self.block, timeout=None, strict=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def __aenter__(self) -> RateLimiter:
        await self._atake(None, self._label(), block=self.block, timeout=None, strict=True)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    # -- decorator ----------------------------------------------------------------------

    def __call__(self, func: F, /) -> F:
        """Count every call of ``func`` against this limiter (sync or ``async def``)."""
        return self._wrap(func, None)

    def _wrap(
        self,
        func: F,
        key: Callable[..., Hashable] | None,
        instances: _Buckets[RateLimiter] | None = None,
    ) -> F:
        label = self.name or _core.qualname(func)
        block = self.block

        def pick(args: tuple[Any, ...], kwargs: dict[str, Any]) -> RateLimiter:
            return self if instances is None else instances.get(args, kwargs)

        def for_instance(instance: object) -> RateLimiter | None:
            if instances is None:
                return self
            return instances.find((instance,), {})

        if _core.is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                k = None if key is None else key(*args, **kwargs)
                await pick(args, kwargs)._atake(k, label, block=block, timeout=None, strict=True)
                return await func(*args, **kwargs)

            async_wrapper.__rate_limiter__ = self  # type: ignore[attr-defined]
            async_wrapper.__rate_limiter_for__ = for_instance  # type: ignore[attr-defined]
            async_wrapper.__python_wrap_layer__ = ("limiter", self)  # type: ignore[attr-defined]
            return cast(F, async_wrapper)

        if self._async_backend:
            raise TypeError(
                f"{label} is synchronous but this limiter has an async backend; "
                "use RedisWindow for synchronous code"
            )

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            k = None if key is None else key(*args, **kwargs)
            pick(args, kwargs)._take(k, label, block=block, timeout=None, strict=True)
            return func(*args, **kwargs)

        wrapper.__rate_limiter__ = self  # type: ignore[attr-defined]
        wrapper.__rate_limiter_for__ = for_instance  # type: ignore[attr-defined]
        wrapper.__python_wrap_layer__ = ("limiter", self)  # type: ignore[attr-defined]
        return cast(F, wrapper)


def limiter_of(func: Callable[..., Any], /, instance: object = None) -> RateLimiter | None:
    """The `RateLimiter` behind a function decorated with `rate_limit` (or a limiter).

    `rate_limit` keeps the decorated function's exact type, so its limiter is reached
    through this function rather than an attribute. Decorators applied on top with
    `functools.wraps` keep it reachable; bound methods work too.

    Args:
        func: A function decorated with `rate_limit` or with a `RateLimiter`.
        instance: For a method decorated with ``rate_limit(..., per_instance=True)``, the
            instance whose limiter to return (there is one per instance; without
            ``instance`` the result is the shared template, which is never used for
            calls). Ignored otherwise.

    Returns:
        Its limiter (the outermost one, if several), or `None` if it has none (or, with
        ``instance``, if that instance has not called the method yet).

    Example:
        ```python
        from python_wrap.concurrency import limiter_of, rate_limit

        @rate_limit(1, 60.0)
        def ping() -> str:
            return "pong"

        assert ping() == "pong"
        limiter = limiter_of(ping)
        assert limiter is not None and not limiter.try_acquire()
        limiter.reset()  # e.g. between tests
        assert ping() == "pong"
        ```
    """
    limiter = getattr(func, "__rate_limiter__", None)
    if not isinstance(limiter, RateLimiter):
        return None
    if instance is not None:
        for_instance = getattr(func, "__rate_limiter_for__", None)
        if callable(for_instance):
            found = for_instance(instance)
            return found if isinstance(found, RateLimiter) else None
    return limiter


def rate_limit(
    calls: int | None = None,
    period: float | None = None,
    *,
    block: bool = False,
    key: Callable[..., Hashable] | None = None,
    limits: Iterable[Limit] | None = None,
    per_instance: bool = False,
) -> Callable[[F], F]:
    """Allow at most ``calls`` calls per ``period`` seconds.

    Uses a sliding-window log: a call is allowed when fewer than ``calls`` calls started
    in the last ``period`` seconds. Unlike a fixed window, this never lets a burst of
    ``2 * calls`` through at a window boundary. Rejected (or waiting) calls are not
    counted. The limiter is thread-safe and is shared by every caller of the decorated
    function — including every instance, for methods — unless ``key`` or ``per_instance``
    splits it.

    Each decorated function gets its own `RateLimiter`, reachable with
    ``limiter_of(func)`` (e.g. to ``.reset()`` it between tests). To share one budget
    between several functions, create a `RateLimiter` and use it as the decorator. Pass
    ``limits=[(5, 1.0), (600, 900.0)]`` to enforce several limits at once (all must
    allow a call). While ``python_wrap.testing`` disables rate limits, every call goes
    through; under ``fast_mode()`` a ``block=True`` call that would wait goes through at
    once (and is counted). Emits ``rate_limit.rejected`` and ``rate_limit.waited``.

    Args:
        calls: Maximum number of calls per window; at least 1.
        period: Window length in seconds; must be positive.
        block: If ``False`` (default) an over-limit call raises ``RateLimitExceeded``.
            If ``True`` it waits until allowed (``async def`` functions wait with
            ``await``, never blocking the event loop).
        key: Optional function called with the call's arguments (including ``self`` for
            methods) that returns a hashable bucket key, e.g. a user id. Each key gets
            its own limit.
        limits: Further ``(calls, period)`` pairs, alone or on top of ``calls``/``period``.
        per_instance: For methods: give every instance its own limiter (found by identity
            through a weak reference, so instances stay collectable and need not be
            hashable). Reach one with ``limiter_of(obj.method, instance=obj)``. Instances
            must be weak-referenceable (a ``__slots__`` class must list ``'__weakref__'``).

    Returns:
        A decorator that preserves the wrapped function's signature.

    Raises:
        TypeError: If no limit is given, or only one of ``calls`` and ``period``. At call
            time with ``per_instance``, an instance that cannot be weakly referenced.
        ValueError: If ``calls < 1`` or ``period <= 0`` (for any limit).
        RateLimitExceeded: From the wrapped call when over the limit and ``block`` is
            false. Its ``retry_after`` attribute says how many seconds until a slot frees.

    Example:
        >>> @rate_limit(2, 1.0, key=lambda user, *_: user)
        ... def send(user: str, text: str) -> None: ...
        >>> @rate_limit(limits=[(5, 1.0), (600, 900.0)])
        ... def strava_call() -> None: ...
    """
    all_limits = _limits(calls, period, limits)

    def decorate(func: F) -> F:
        limiter = RateLimiter(limits=all_limits, block=block)
        if not per_instance:
            return limiter._wrap(func, key)
        instances = _Buckets(
            lambda: RateLimiter(limits=all_limits, block=block),
            per_instance=True,
            what="rate_limit",
        )
        return limiter._wrap(func, key, instances)

    return decorate


# ---------------------------------------------------------------------------------------
# run_in_thread
# ---------------------------------------------------------------------------------------
