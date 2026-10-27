"""Bulkheads: cap how many calls run at once, and how many may queue for a slot.

* `Bulkhead` — a reusable limit, used as a decorator, `with bulkhead:` or
  `async with bulkhead:`.
* `limit_concurrency` — decorator giving each function its own `Bulkhead`.

A bulkhead protects something with limited capacity (a database pool, a slow API, a
CPU-heavy section) and fails fast with `BulkheadFull` instead of letting an unbounded
queue build up.
"""

from __future__ import annotations

import asyncio
import functools
import threading
import weakref
from collections.abc import Callable
from types import TracebackType
from typing import Any, Concatenate, ParamSpec, Protocol, TypeVar, cast, overload

from python_wrap import _core
from python_wrap._core import F, P, R
from python_wrap.aio import _cancel_and_settle
from python_wrap.errors import BulkheadFull

__all__ = ["Bulkhead", "Limited", "limit_concurrency"]

R_co = TypeVar("R_co", covariant=True)
T = TypeVar("T")
P2 = ParamSpec("P2")


class _Pool:
    """Counters for one semaphore (the threads' one, or one event loop's)."""

    __slots__ = ("active", "waiting")

    def __init__(self) -> None:
        self.active = 0
        self.waiting = 0


class _AsyncPool(_Pool):
    __slots__ = ("semaphore",)

    def __init__(self, size: int) -> None:
        super().__init__()
        self.semaphore = asyncio.Semaphore(size)


async def _acquire_within(semaphore: asyncio.Semaphore, seconds: float) -> bool:
    """Acquire ``semaphore`` unless ``seconds`` pass first (timed with ``_core``).

    Never leaks a permit: if the acquisition wins at the last moment while we time out or
    are cancelled, the permit is either kept (timeout) or released (cancellation).
    """
    acquire = asyncio.ensure_future(semaphore.acquire())
    timer = asyncio.ensure_future(_core.async_sleep(seconds))
    try:
        await asyncio.wait((acquire, timer), return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        await _cancel_and_settle((acquire, timer))
        if _won(acquire):
            semaphore.release()
        raise
    if not acquire.done():
        acquire.cancel()
    await _cancel_and_settle((timer, acquire))
    return _won(acquire)


def _won(acquire: asyncio.Future[Any]) -> bool:
    return acquire.done() and not acquire.cancelled() and acquire.exception() is None


class Bulkhead:
    """Allow at most `max_concurrent` calls at once; reject when too many are waiting.

    Use one instance to guard several functions or code blocks together:

    * as a decorator, on sync or `async def` functions (and methods);
    * as `with bulkhead:` in threaded code;
    * as `async with bulkhead:` in async code.

    Sync callers share a `threading.BoundedSemaphore`. Async callers use an
    `asyncio.Semaphore` per event loop (asyncio primitives cannot be shared across
    loops). So the limit applies separately to threads and to each running loop; use
    one bulkhead per side if you mix them. Never use `with bulkhead:` inside a coroutine:
    waiting for a slot would block the event loop.

    A caller that finds no free slot queues for one, unless `max_waiting` callers are
    already queued (then it gets `BulkheadFull` immediately). A queued caller gives up
    with `BulkheadFull` after `timeout` seconds. Each rejection emits the
    `bulkhead.rejected` event with the `active` and `waiting` counts at that moment.

    Args:
        max_concurrent: Maximum number of calls running at once (>= 1).
        max_waiting: Maximum number of callers queued for a slot; `None` means no
            limit, `0` means never wait (fail fast when full).
        timeout: Maximum seconds to wait for a slot; `None` waits forever.
        name: Label used in error messages and `repr`.

    Raises:
        ValueError: An argument is out of range.

    Example:
        ```python
        import asyncio
        from python_wrap.bulkhead import Bulkhead
        from python_wrap.errors import BulkheadFull

        db = Bulkhead(2, max_waiting=0, name="db")

        @db
        async def query(n: int) -> int:
            await asyncio.sleep(0.01)
            return n

        async def main() -> None:
            results = await asyncio.gather(*(query(n) for n in range(3)),
                                           return_exceptions=True)
            assert results[:2] == [0, 1]
            assert isinstance(results[2], BulkheadFull)  # two running, none may wait

        asyncio.run(main())
        ```
    """

    def __init__(
        self,
        max_concurrent: int,
        *,
        max_waiting: int | None = None,
        timeout: float | None = None,
        name: str | None = None,
    ) -> None:
        if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int):
            raise ValueError(f"max_concurrent must be an integer, got {max_concurrent!r}")
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
        if max_waiting is not None and max_waiting < 0:
            raise ValueError(f"max_waiting must be >= 0 or None, got {max_waiting}")
        if timeout is not None and timeout < 0:
            raise ValueError(f"timeout must be >= 0 or None, got {timeout}")
        self.max_concurrent = max_concurrent
        self.max_waiting = max_waiting
        self.timeout = timeout
        self.name = name
        self._lock = threading.Lock()
        self._thread_semaphore = threading.BoundedSemaphore(max_concurrent)
        self._thread_pool = _Pool()
        self._loop_pools: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _AsyncPool] = (
            weakref.WeakKeyDictionary()
        )

    # -- introspection ------------------------------------------------------------------

    def _pools(self) -> list[_Pool]:
        return [self._thread_pool, *self._loop_pools.values()]

    @property
    def active(self) -> int:
        """Number of calls currently holding a slot (threads and every loop together)."""
        with self._lock:
            return sum(pool.active for pool in self._pools())

    @property
    def waiting(self) -> int:
        """Number of callers currently queued for a slot (threads and every loop)."""
        with self._lock:
            return sum(pool.waiting for pool in self._pools())

    def __repr__(self) -> str:
        return (
            f"Bulkhead(name={self.name!r}, max_concurrent={self.max_concurrent}, "
            f"active={self.active}, waiting={self.waiting})"
        )

    # -- bookkeeping --------------------------------------------------------------------

    def _full(self, why: str, active: int, waiting: int) -> BulkheadFull:
        """Build the rejection and emit ``bulkhead.rejected`` (call without the lock)."""
        _core.emit("bulkhead.rejected", self.name or "bulkhead", active=active, waiting=waiting)
        label = f"bulkhead {self.name!r}" if self.name else "bulkhead"
        return BulkheadFull(
            f"{label} is full: {why}", name=self.name, max_concurrent=self.max_concurrent
        )

    def _start_waiting(self, pool: _Pool) -> None:
        with self._lock:
            active, waiting = pool.active, pool.waiting
            full = self.max_waiting is not None and waiting >= self.max_waiting
            if not full:
                pool.waiting += 1
        if full:
            raise self._full(
                f"{self.max_concurrent} calls running and "
                f"{waiting} waiting (max_waiting={self.max_waiting})",
                active,
                waiting,
            )

    def _stop_waiting(self, pool: _Pool) -> None:
        with self._lock:
            pool.waiting -= 1

    def _enter(self, pool: _Pool) -> None:
        with self._lock:
            pool.active += 1

    def _exit(self, pool: _Pool) -> None:
        with self._lock:
            pool.active -= 1

    def _timed_out(self, pool: _Pool) -> BulkheadFull:
        with self._lock:
            active, waiting = pool.active, pool.waiting
        return self._full(f"no slot freed up within {self.timeout}s", active, waiting)

    # -- sync ---------------------------------------------------------------------------

    def acquire(self) -> None:
        """Take a slot, blocking the thread while waiting. Prefer `with bulkhead:`.

        Raises:
            BulkheadFull: The queue is full, or `timeout` expired.
        """
        pool = self._thread_pool
        semaphore = self._thread_semaphore
        if not semaphore.acquire(blocking=False):
            self._start_waiting(pool)
            try:
                if self.timeout is None:
                    got = semaphore.acquire()
                else:
                    got = semaphore.acquire(timeout=self.timeout)
            finally:
                self._stop_waiting(pool)
            if not got:
                raise self._timed_out(pool)
        self._enter(pool)

    def release(self) -> None:
        """Give back a slot taken with `acquire`."""
        # Decrement `active` before freeing the permit, so a caller that takes the slot
        # right away never sees more than `max_concurrent` active.
        pool = self._thread_pool
        self._exit(pool)
        try:
            self._thread_semaphore.release()  # ValueError if nothing was acquired
        except ValueError:
            self._enter(pool)
            raise

    def __enter__(self) -> Bulkhead:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()

    # -- async --------------------------------------------------------------------------

    def _loop_pool(self) -> _AsyncPool:
        loop = asyncio.get_running_loop()
        with self._lock:
            pool = self._loop_pools.get(loop)
            if pool is None:
                pool = self._loop_pools[loop] = _AsyncPool(self.max_concurrent)
            return pool

    async def acquire_async(self) -> None:
        """Take a slot in the running event loop. Prefer `async with bulkhead:`.

        Raises:
            BulkheadFull: The queue is full, or `timeout` expired.
        """
        pool = self._loop_pool()
        semaphore = pool.semaphore
        if semaphore.locked():
            self._start_waiting(pool)
            try:
                if self.timeout is None:
                    await semaphore.acquire()
                elif not await _acquire_within(semaphore, self.timeout):
                    raise self._timed_out(pool)
            finally:
                self._stop_waiting(pool)
        else:
            await semaphore.acquire()  # a slot is free: returns without suspending
        self._enter(pool)

    def release_async(self) -> None:
        """Give back a slot taken with `acquire_async` (from the same event loop)."""
        pool = self._loop_pool()
        self._exit(pool)
        pool.semaphore.release()

    async def __aenter__(self) -> Bulkhead:
        await self.acquire_async()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release_async()

    # -- decorator ----------------------------------------------------------------------

    def __call__(self, func: F, /) -> F:
        """Guard every call of `func` with this bulkhead (sync or `async def`)."""
        if _core.is_async(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                async with self:
                    return await func(*args, **kwargs)

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with self:
                return func(*args, **kwargs)

        return cast(F, wrapper)


class Limited(Protocol[P, R_co]):
    """A function wrapped by `limit_concurrency`; `.bulkhead` is its own `Bulkhead`.

    Binds `self` and `cls` (including under a `@classmethod` stacked above) like
    `python_wrap.caching.Memoized`, with the same typing limitations.
    """

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...

    @overload
    def __get__(
        self: Limited[Concatenate[type[T], P2], R_co], instance: None, owner: type[T]
    ) -> _LimitedOnClass[P, P2, R_co]: ...
    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Limited[P, R_co]: ...
    @overload
    def __get__(
        self: Limited[Concatenate[Any, P2], R_co], instance: object, owner: type | None = None
    ) -> Limited[P2, R_co]: ...

    @property
    def bulkhead(self) -> Bulkhead:
        """The function's own `Bulkhead` (inspect `.active` and `.waiting`)."""
        ...


class _LimitedOnClass(Limited[P, R_co], Protocol[P, P2, R_co]):
    """`Limited` read from a class: accepts the call with and without the first argument.

    Returned for class access (`Cls.attr`) when the first parameter could be `cls`.
    pyright only picks it under `@classmethod`; mypy cannot tell a classmethod from an
    instance or static method there, so both call forms are accepted.
    """

    @overload
    def __call__(self, *args: P2.args, **kwargs: P2.kwargs) -> R_co: ...
    @overload
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...


def limit_concurrency(n: int, /) -> Callable[[Callable[P, R]], Limited[P, R]]:
    """Let at most `n` calls of the decorated function run at once; the rest wait.

    Each decorated function gets its own `Bulkhead(n)` (no queue limit, no timeout),
    exposed as `wrapped.bulkhead` for introspection (`.active`, `.waiting`). For a limit
    shared by several functions, or for `max_waiting`/`timeout`, use a `Bulkhead`
    directly. Works on sync and `async def` functions and on methods (the limit is then
    shared by every instance).

    Args:
        n: Maximum number of concurrent calls (>= 1).

    Returns:
        A decorator.

    Raises:
        ValueError: `n` is not a positive integer.

    Example:
        ```python
        import asyncio
        from python_wrap.bulkhead import limit_concurrency

        peak = 0

        @limit_concurrency(2)
        async def crawl(page: int) -> int:
            global peak
            peak = max(peak, crawl.bulkhead.active)
            await asyncio.sleep(0)
            return page

        async def main() -> None:
            assert await asyncio.gather(*(crawl(p) for p in range(6))) == list(range(6))
            assert peak == 2

        asyncio.run(main())
        ```
    """
    Bulkhead(n)  # validate eagerly, at decoration time

    def decorate(func: Callable[P, R]) -> Limited[P, R]:
        bulkhead = Bulkhead(n, name=_core.qualname(func))
        wrapped = cast(Any, bulkhead(func))
        wrapped.bulkhead = bulkhead
        return cast(Limited[P, R], wrapped)

    return decorate
