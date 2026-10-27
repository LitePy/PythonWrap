"""Small asyncio helpers that every async project ends up writing.

* `gather_limit` — `asyncio.gather` with at most `limit` awaitables running at once.
* `map_async` — apply an async function to every item of a (possibly huge or async)
  iterable, at most `limit` at a time, and collect the results.
* `amap_iter` — the streaming form of `map_async`: an async generator of results.
* `race` — the first awaitable to succeed wins; the others are cancelled.
* `run_sync` — run a coroutine from synchronous code (never inside a running loop).

Every helper owns the tasks it creates: when it returns, raises, is cancelled or (for
`amap_iter`) is closed early, every task it started has finished, so no task is left
pending behind your back.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Coroutine,
    Iterable,
    Iterator,
)
from contextlib import aclosing
from typing import Any, Literal, ParamSpec, TypeVar, overload

from python_wrap import _core
from python_wrap.errors import PythonWrapError, TimeoutExpired

__all__ = [
    "RaceFailed",
    "amap_iter",
    "gather_limit",
    "map_async",
    "race",
    "run_sync",
]

T = TypeVar("T")
R = TypeVar("R")
P = ParamSpec("P")

# ---------------------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------------------


def _check_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive integer, got {limit!r}")


async def _await(aw: Awaitable[T]) -> T:
    return await aw


def _start(aw: Awaitable[T]) -> asyncio.Future[T]:
    """Schedule ``aw``. Coroutines become tasks; anything else is awaited in a new task.

    Wrapping futures in a task of our own means cancelling that task also cancels the
    future, and the same future may appear more than once.
    """
    if asyncio.iscoroutine(aw):
        return asyncio.ensure_future(aw)
    return asyncio.ensure_future(_await(aw))


async def _settle(futures: Iterable[asyncio.Future[Any]]) -> None:
    """Wait until every future is done, even if we are cancelled meanwhile.

    A cancellation that arrives while waiting is re-raised once everything has finished,
    so cleanup never leaves tasks behind. Exceptions of the futures are marked as
    retrieved, so asyncio does not log "exception was never retrieved".
    """
    futures = list(futures)
    cancelled = False
    while True:
        waiting = [f for f in futures if not f.done()]
        if not waiting:
            break
        try:
            await asyncio.wait(waiting)
        except asyncio.CancelledError:
            cancelled = True
    for f in futures:
        if not f.cancelled():
            f.exception()
    if cancelled:
        raise asyncio.CancelledError


async def _cancel_and_settle(futures: Iterable[asyncio.Future[Any]]) -> None:
    futures = list(futures)
    for f in futures:
        f.cancel()
    await _settle(futures)


_Next = Callable[[], Awaitable["Awaitable[Any] | None"]]


async def _drive(
    next_awaitable: _Next, limit: int, ordered: bool
) -> AsyncGenerator[tuple[int, asyncio.Future[Any]], None]:
    """Start awaitables pulled from ``next_awaitable`` and yield ``(index, done_future)``.

    At most ``limit`` futures are in flight. With ``ordered`` they are yielded in input
    order and finished-but-not-yet-yielded futures count towards ``limit``, so the reorder
    buffer is bounded. Without it they are yielded as they finish (ties in input order).
    Closing the generator cancels and awaits everything still running.
    """
    pending: dict[asyncio.Future[Any], int] = {}
    ready: dict[int, asyncio.Future[Any]] = {}
    count = 0
    next_out = 0
    exhausted = False
    try:
        while True:
            while not exhausted and len(pending) + len(ready) < limit:
                aw = await next_awaitable()
                if aw is None:
                    exhausted = True
                else:
                    pending[_start(aw)] = count
                    count += 1
            while next_out in ready:
                yield next_out, ready.pop(next_out)
                next_out += 1
            if not pending:
                if exhausted:
                    return
                continue  # everything finished was yielded: pull more input
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for fut in sorted(done, key=pending.__getitem__):
                index = pending.pop(fut)
                if ordered:
                    ready[index] = fut
                else:
                    yield index, fut
    finally:
        await _cancel_and_settle([*pending, *ready.values()])


def _outcome(fut: asyncio.Future[Any], return_exceptions: bool) -> Any:
    if not return_exceptions:
        return fut.result()
    if fut.cancelled():
        return asyncio.CancelledError()
    return fut.exception() or fut.result()


def _discard(aw: Awaitable[Any], leftovers: list[asyncio.Future[Any]]) -> None:
    """Dispose of an awaitable that was never started."""
    if asyncio.iscoroutine(aw):
        aw.close()  # no "coroutine was never awaited" warning
    elif asyncio.isfuture(aw):
        leftovers.append(aw)


# ---------------------------------------------------------------------------------------
# gather_limit
# ---------------------------------------------------------------------------------------


@overload
async def gather_limit(
    *aws: Awaitable[T], limit: int, return_exceptions: Literal[False] = False
) -> list[T]: ...
@overload
async def gather_limit(
    *aws: Awaitable[T], limit: int, return_exceptions: bool
) -> list[T | BaseException]: ...
async def gather_limit(
    *aws: Awaitable[Any], limit: int, return_exceptions: bool = False
) -> list[Any]:
    """Like `asyncio.gather`, but at most `limit` awaitables run at the same time.

    Coroutines are started only when a slot is free. Tasks and futures are already
    running when you pass them, so `limit` cannot hold them back; they are simply awaited
    (and cancelled with the rest on failure).

    Args:
        *aws: Coroutines, tasks or futures (any awaitable).
        limit: Maximum number of awaitables running at once (>= 1).
        return_exceptions: If true, exceptions (and `asyncio.CancelledError` for a child
            cancelled from outside) are returned in the result list instead of raised.

    Returns:
        The results, in the order of `aws` (not completion order).

    Raises:
        ValueError: `limit` is not a positive integer.
        Exception: With `return_exceptions=False`, the first exception raised by any
            awaitable. Everything still running is cancelled and awaited first, and
            coroutines that never started are closed.

    Example:
        ```python
        import asyncio
        from python_wrap.aio import gather_limit

        async def square(n: int) -> int:
            await asyncio.sleep(0)
            return n * n

        async def main() -> None:
            results = await gather_limit(*(square(n) for n in range(10)), limit=3)
            assert results == [n * n for n in range(10)]

        asyncio.run(main())
        ```
    """
    _check_limit(limit)
    results: list[Any] = [None] * len(aws)
    source: Iterator[Awaitable[Any]] = iter(aws)

    async def next_awaitable() -> Awaitable[Any] | None:
        return next(source, None)

    leftovers: list[asyncio.Future[Any]] = []
    try:
        async with aclosing(_drive(next_awaitable, limit, ordered=False)) as finished:
            async for index, fut in finished:
                results[index] = _outcome(fut, return_exceptions)
    finally:
        for aw in source:
            _discard(aw, leftovers)
        if leftovers:
            await _cancel_and_settle(leftovers)
    return results


# ---------------------------------------------------------------------------------------
# map_async / amap_iter
# ---------------------------------------------------------------------------------------


async def _call(fn: Callable[[T], Awaitable[R]], item: T) -> R:
    return await fn(item)


def _items_source(fn: Callable[[T], Awaitable[R]], items: Iterable[T] | AsyncIterable[T]) -> _Next:
    if isinstance(items, AsyncIterable):
        ait: AsyncIterator[T] = aiter(items)

        async def next_async() -> Awaitable[Any] | None:
            try:
                item = await anext(ait)
            except StopAsyncIteration:
                return None
            return _call(fn, item)

        return next_async

    it: Iterator[T] = iter(items)
    sentinel = object()

    async def next_sync() -> Awaitable[Any] | None:
        item = next(it, sentinel)
        return None if item is sentinel else _call(fn, item)  # type: ignore[arg-type]

    return next_sync


@overload
def amap_iter(
    fn: Callable[[T], Awaitable[R]],
    items: Iterable[T] | AsyncIterable[T],
    *,
    limit: int,
    ordered: bool = True,
    return_exceptions: Literal[False] = False,
) -> AsyncGenerator[R, None]: ...
@overload
def amap_iter(
    fn: Callable[[T], Awaitable[R]],
    items: Iterable[T] | AsyncIterable[T],
    *,
    limit: int,
    ordered: bool = True,
    return_exceptions: bool,
) -> AsyncGenerator[R | BaseException, None]: ...
def amap_iter(
    fn: Callable[[T], Awaitable[R]],
    items: Iterable[T] | AsyncIterable[T],
    *,
    limit: int,
    ordered: bool = True,
    return_exceptions: bool = False,
) -> AsyncGenerator[Any, None]:
    """Apply `fn` to every item, at most `limit` at a time, yielding results as they come.

    The input is consumed lazily: an item is pulled only when a slot is free, so an
    infinite generator is fine. `fn` is called inside the new task, so even an exception
    raised synchronously by `fn` counts as that item's failure.

    * `ordered=True`: results come out in input order. Results that finish early wait in
      a buffer, and buffered results count towards `limit`, so memory stays bounded; a
      slow item holds back the ones behind it. An exception surfaces when its turn comes.
    * `ordered=False`: results come out as soon as they finish; an exception surfaces at
      once.

    Breaking out of the loop, calling `aclose()`, or cancelling the consuming task cancels
    every call still running and waits for it. To make cleanup immediate, close the
    generator explicitly (`contextlib.aclosing`) rather than relying on garbage
    collection. The input iterator itself is not closed.

    Args:
        fn: Async function (or any callable returning an awaitable) of one item.
        items: A sync or async iterable.
        limit: Maximum number of calls in flight (>= 1).
        ordered: Yield in input order (default) or in completion order.
        return_exceptions: Yield exceptions as values instead of raising them.

    Returns:
        An async generator of results.

    Raises:
        ValueError: `limit` is not a positive integer.

    Example:
        ```python
        import asyncio
        from contextlib import aclosing
        from itertools import count
        from python_wrap.aio import amap_iter

        async def double(n: int) -> int:
            await asyncio.sleep(0)
            return 2 * n

        async def main() -> None:
            seen = []
            async with aclosing(amap_iter(double, count(), limit=4)) as results:
                async for value in results:  # an infinite input is fine
                    seen.append(value)
                    if len(seen) == 5:
                        break
            assert seen == [0, 2, 4, 6, 8]

        asyncio.run(main())
        ```
    """
    _check_limit(limit)
    source = _items_source(fn, items)

    async def generate() -> AsyncGenerator[Any, None]:
        async with aclosing(_drive(source, limit, ordered)) as finished:
            async for _, fut in finished:
                yield _outcome(fut, return_exceptions)

    return generate()


@overload
async def map_async(
    fn: Callable[[T], Awaitable[R]],
    items: Iterable[T] | AsyncIterable[T],
    *,
    limit: int,
    ordered: bool = True,
    return_exceptions: Literal[False] = False,
) -> list[R]: ...
@overload
async def map_async(
    fn: Callable[[T], Awaitable[R]],
    items: Iterable[T] | AsyncIterable[T],
    *,
    limit: int,
    ordered: bool = True,
    return_exceptions: bool,
) -> list[R | BaseException]: ...
async def map_async(
    fn: Callable[[T], Awaitable[R]],
    items: Iterable[T] | AsyncIterable[T],
    *,
    limit: int,
    ordered: bool = True,
    return_exceptions: bool = False,
) -> list[Any]:
    """Apply `fn` to every item, at most `limit` at a time, and return all the results.

    Items are pulled lazily, so at most `limit` calls (and items) are in flight however
    long the input is. Unlike `amap_iter`, an exception is raised as soon as any call
    fails (with `return_exceptions=False`): the other calls are cancelled and awaited and
    no further items are pulled.

    Args:
        fn: Async function (or any callable returning an awaitable) of one item.
        items: A sync or async iterable.
        limit: Maximum number of calls in flight (>= 1).
        ordered: Return results in input order (default) or in completion order.
        return_exceptions: Put exceptions in the list instead of raising them.

    Returns:
        A list with one result per item.

    Raises:
        ValueError: `limit` is not a positive integer.
        Exception: The first exception raised by `fn` (with `return_exceptions=False`).

    Example:
        ```python
        import asyncio
        from python_wrap.aio import map_async

        async def fetch(url: str) -> int:
            await asyncio.sleep(0)
            return len(url)

        async def main() -> None:
            urls = ["https://a.example", "https://bb.example"]
            sizes = await map_async(fetch, urls, limit=10)
            assert sizes == [17, 18]

        asyncio.run(main())
        ```
    """
    _check_limit(limit)
    source = _items_source(fn, items)
    by_index: dict[int, Any] = {}
    async with aclosing(_drive(source, limit, ordered=False)) as finished:
        async for index, fut in finished:
            by_index[index] = _outcome(fut, return_exceptions)
    if ordered:
        return [by_index[i] for i in range(len(by_index))]
    return list(by_index.values())


# ---------------------------------------------------------------------------------------
# race
# ---------------------------------------------------------------------------------------

if sys.version_info >= (3, 11):

    class RaceFailed(ExceptionGroup[Exception], PythonWrapError):  # noqa: F821
        """Every contestant of `race` failed.

        On Python 3.11+ this is an `ExceptionGroup` (so `except*` works) and a
        `PythonWrapError`. On 3.10 it is a plain `PythonWrapError`. On every version,
        `.exceptions` is a tuple of the errors in input order.
        """

        def __new__(cls, message: str, exceptions: Iterable[Exception]) -> RaceFailed:
            return super().__new__(cls, message, list(exceptions))

        def __init__(self, message: str, exceptions: Iterable[Exception]) -> None:
            super().__init__(message, list(exceptions))

        def derive(self, excs: Any) -> Any:
            return RaceFailed(self.message, excs)

else:

    class RaceFailed(PythonWrapError):
        """Every contestant of `race` failed.

        On Python 3.11+ this is an `ExceptionGroup` (so `except*` works) and a
        `PythonWrapError`. On 3.10 it is a plain `PythonWrapError`. On every version,
        `.exceptions` is a tuple of the errors in input order.
        """

        def __init__(self, message: str, exceptions: Iterable[Exception]) -> None:
            super().__init__(message)
            self.message = message
            self.exceptions: tuple[Exception, ...] = tuple(exceptions)


async def race(*aws: Awaitable[T], timeout: float | None = None) -> T:
    """Run every awaitable at once and return the result of the first to succeed.

    As soon as one succeeds, the others are cancelled and awaited before `race`
    returns. A contestant that fails does not end the race; the others keep running.
    If several finish in the same event-loop step, the earliest in `aws` wins.

    Args:
        *aws: Coroutines, tasks or futures; at least one.
        timeout: Give up after this many seconds (measured with `_core.async_sleep`, so
            the test clock applies). `None` waits forever.

    Returns:
        The winner's result.

    Raises:
        ValueError: No awaitables were given.
        RaceFailed: Every contestant raised an exception; `.exceptions` holds them in
            input order. Contestants cancelled from outside are left out of it (if all
            of them were cancelled, `asyncio.CancelledError` is raised instead).
        TimeoutExpired: Nobody succeeded within `timeout` (also a `TimeoutError`).
        BaseException: A contestant raised `KeyboardInterrupt`/`SystemExit` (or similar):
            the others are cancelled and it propagates at once.

    Example:
        ```python
        import asyncio
        from python_wrap.aio import race

        async def mirror(name: str, delay: float) -> str:
            await asyncio.sleep(delay)
            return name

        async def main() -> None:
            winner = await race(mirror("slow", 1), mirror("fast", 0), timeout=5)
            assert winner == "fast"

        asyncio.run(main())
        ```
    """
    if not aws:
        raise ValueError("race() needs at least one awaitable")
    contestants: list[asyncio.Future[T]] = []
    timer: asyncio.Future[None] | None = None
    errors: dict[int, Exception] = {}
    try:
        for aw in aws:
            contestants.append(_start(aw))
        order: dict[asyncio.Future[Any], int] = {fut: i for i, fut in enumerate(contestants)}
        if timeout is not None:
            timer = asyncio.ensure_future(_core.async_sleep(timeout))
        pending: set[asyncio.Future[Any]] = set(contestants)
        while pending:
            watch = pending if timer is None else pending | {timer}
            done, _ = await asyncio.wait(watch, return_when=asyncio.FIRST_COMPLETED)
            finished = sorted((f for f in done if f is not timer), key=order.__getitem__)
            for fut in finished:
                pending.discard(fut)
                if fut.cancelled():
                    continue
                exc = fut.exception()
                if exc is None:
                    return contestants[order[fut]].result()
                if not isinstance(exc, Exception):
                    raise exc
                errors[order[fut]] = exc
            if timer is not None and timer in done and pending:
                raise TimeoutExpired(
                    f"race() found no winner within {timeout}s", seconds=float(timeout or 0)
                )
    finally:
        await _cancel_and_settle([*contestants, *([timer] if timer else [])])
    if not errors:
        raise asyncio.CancelledError
    failures = [errors[i] for i in sorted(errors)]
    raise RaceFailed(f"all {len(failures)} contestants of race() failed", failures)


# ---------------------------------------------------------------------------------------
# run_sync
# ---------------------------------------------------------------------------------------


@overload
def run_sync(coro: Coroutine[Any, Any, T], /) -> T: ...
@overload
def run_sync(fn: Callable[P, Awaitable[T]], /, *args: P.args, **kwargs: P.kwargs) -> T: ...
def run_sync(coro_or_fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Run async code from synchronous code and return its result.

    Uses `asyncio.run`, so a fresh event loop is created and closed for the call. There
    is deliberately no "nested event loop" support: calling this while an event loop is
    running in the current thread raises `RuntimeError` (use `await` there instead, or
    run the sync caller in a worker thread with `asyncio.to_thread`).

    Args:
        coro_or_fn: A coroutine object, or an async function to call with `args`/`kwargs`.
        *args: Positional arguments for the function.
        **kwargs: Keyword arguments for the function.

    Returns:
        Whatever the coroutine returns.

    Raises:
        RuntimeError: An event loop is already running in this thread.
        TypeError: Arguments were given with a coroutine object, or the function did not
            return an awaitable.

    Example:
        ```python
        import asyncio
        from python_wrap.aio import run_sync

        async def add(a: int, b: int) -> int:
            await asyncio.sleep(0)
            return a + b

        assert run_sync(add, 1, 2) == 3
        assert run_sync(add(3, 4)) == 7
        ```
    """
    is_coro = asyncio.iscoroutine(coro_or_fn)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        if is_coro:
            coro_or_fn.close()
        raise RuntimeError(
            "run_sync() cannot be called while an event loop is running in this thread; "
            "await the coroutine instead"
        )
    if is_coro:
        if args or kwargs:
            coro_or_fn.close()
            raise TypeError("run_sync() takes no extra arguments when given a coroutine")
        return asyncio.run(coro_or_fn)
    if not callable(coro_or_fn):
        raise TypeError(f"run_sync() needs a coroutine or an async callable, got {coro_or_fn!r}")
    result = coro_or_fn(*args, **kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    if inspect.isawaitable(result):
        return asyncio.run(_await(result))
    raise TypeError(f"{_core.qualname(coro_or_fn)}() did not return an awaitable")
