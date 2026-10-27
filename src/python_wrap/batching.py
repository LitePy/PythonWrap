"""Coalesce many single-item calls into one batched call: `batch_calls`.

This is the DataLoader pattern. You write a function that takes a *list* of items and
returns their results; `batch_calls` turns it into a function that takes *one* item. Calls
that arrive close together are collected and sent to your function as a single batch.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import threading
import warnings
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Hashable, Mapping, Sequence
from typing import Any, Concatenate, ParamSpec, Protocol, TypeGuard, TypeVar, cast, overload

from python_wrap import _core
from python_wrap.errors import BatchError

__all__ = ["AsyncBatched", "Batched", "batch_calls"]

P = ParamSpec("P")
P2 = ParamSpec("P2")
T = TypeVar("T")
R = TypeVar("R")
S = TypeVar("S")
_O = TypeVar("_O")
R_co = TypeVar("R_co", covariant=True)

_GroupKey = tuple[int, ...]


class Batched(Protocol[P, R_co]):
    """A sync function wrapped by `batch_calls`: call it with one item, get its result."""

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...

    @overload
    def __get__(
        self: Batched[Concatenate[type[_O], P2], R_co], instance: None, owner: type[_O]
    ) -> Batched[P2, R_co]: ...  # under @classmethod: bind the class
    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Batched[P, R_co]: ...
    @overload
    def __get__(
        self: Batched[Concatenate[type[_O], P2], R_co], instance: _O, owner: type | None = None
    ) -> Batched[P2, R_co]: ...
    @overload
    def __get__(
        self: Batched[Concatenate[_O, P2], R_co], instance: _O, owner: type | None = None
    ) -> Batched[P2, R_co]: ...
    @overload
    def __get__(self, instance: object, owner: type | None = None) -> Batched[P, R_co]: ...

    @property
    def pending(self) -> int:
        """Number of distinct items queued and not yet sent to the batch function."""
        ...

    def flush(self) -> None:
        """Send every queued batch now, in the calling thread, and wait for it."""
        ...


class AsyncBatched(Protocol[P, R_co]):
    """An `async def` function wrapped by `batch_calls`: `await loader(item)`."""

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> Coroutine[Any, Any, R_co]: ...

    @overload
    def __get__(
        self: AsyncBatched[Concatenate[type[_O], P2], R_co], instance: None, owner: type[_O]
    ) -> AsyncBatched[P2, R_co]: ...  # under @classmethod: bind the class
    @overload
    def __get__(self, instance: None, owner: type | None = None) -> AsyncBatched[P, R_co]: ...
    @overload
    def __get__(
        self: AsyncBatched[Concatenate[type[_O], P2], R_co], instance: _O, owner: type | None = None
    ) -> AsyncBatched[P2, R_co]: ...
    @overload
    def __get__(
        self: AsyncBatched[Concatenate[_O, P2], R_co], instance: _O, owner: type | None = None
    ) -> AsyncBatched[P2, R_co]: ...
    @overload
    def __get__(self, instance: object, owner: type | None = None) -> AsyncBatched[P, R_co]: ...

    @property
    def pending(self) -> int:
        """Number of distinct items queued and not yet sent to the batch function."""
        ...

    def aflush(self) -> Coroutine[Any, Any, None]:
        """Send every batch queued on the running loop now and wait for them to finish."""
        ...


class _BatchDecorator(Protocol):
    @overload
    def __call__(
        self, func: Callable[[list[T]], Awaitable[Sequence[R] | Mapping[Any, R]]], /
    ) -> AsyncBatched[[T], R]: ...
    @overload
    def __call__(
        self, func: Callable[[S, list[T]], Awaitable[Sequence[R] | Mapping[Any, R]]], /
    ) -> AsyncBatched[[S, T], R]: ...
    @overload
    def __call__(
        self, func: Callable[[list[T]], Sequence[R] | Mapping[Any, R]], /
    ) -> Batched[[T], R]: ...
    @overload
    def __call__(
        self, func: Callable[[S, list[T]], Sequence[R] | Mapping[Any, R]], /
    ) -> Batched[[S, T], R]: ...


# --------------------------------------------------------------------------- shared


def _is_mapping(value: object) -> TypeGuard[Mapping[Any, Any]]:
    return isinstance(value, Mapping)


def _is_sequence(value: object) -> TypeGuard[Sequence[Any]]:
    return isinstance(value, Sequence)


def _split(name: str, result: object, keys: list[Hashable]) -> list[tuple[bool, Any]]:
    """Match a batch result to its keys: ``(True, value)`` or ``(False, BatchError)``."""
    if _is_mapping(result):
        mapping = result
        return [
            (True, mapping[k])
            if k in mapping
            else (False, BatchError(f"{name}: the batch result has no entry for key {k!r}"))
            for k in keys
        ]
    if isinstance(result, (str, bytes, bytearray)) or not _is_sequence(result):
        message = (
            f"{name}: the batch function must return a sequence or a mapping, "
            f"got {type(result).__name__}"
        )
        return [(False, BatchError(message)) for _ in keys]
    values = result
    if len(values) != len(keys):
        message = f"{name}: the batch function returned {len(values)} results for {len(keys)} items"
        return [(False, BatchError(message)) for _ in keys]
    return [(True, v) for v in values]


def _split_args(name: str, args: tuple[Any, ...]) -> tuple[tuple[Any, ...], Any]:
    if not args:
        raise TypeError(f"{name}() takes one item to load")
    return args[:-1], args[-1]


class _Counter:
    """Thread-safe count of queued items, mirrored onto ``wrapper.pending``."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value = 0
        self.wrapper: Any = None

    def add(self, n: int) -> None:
        with self.lock:
            self.value += n
            self.wrapper.pending = self.value


# --------------------------------------------------------------------------- sync


class _SyncEntry:
    __slots__ = ("future", "item")

    def __init__(self, item: Any) -> None:
        self.item = item
        self.future: concurrent.futures.Future[Any] = concurrent.futures.Future()


class _SyncBatch:
    __slots__ = ("entries", "group", "prefix")

    def __init__(self, group: _GroupKey, prefix: tuple[Any, ...]) -> None:
        self.group = group
        self.prefix = prefix
        self.entries: dict[Hashable, _SyncEntry] = {}


def _batch_sync(
    func: Callable[..., Any],
    max_size: int,
    max_wait: float,
    key_of: Callable[[Any], Hashable],
) -> Callable[..., Any]:
    name = _core.qualname(func)
    lock = threading.Lock()
    open_batches: dict[_GroupKey, _SyncBatch] = {}
    counter = _Counter()
    warned = False

    def warn_if_in_event_loop() -> None:
        nonlocal warned
        if asyncio._get_running_loop() is None:
            return
        warned = True
        warnings.warn(
            f"{name} is a sync batch_calls loader called from a running event loop: it "
            "blocks the loop while it waits for its batch (up to max_wait) and returns a "
            "plain value, not a coroutine, so it cannot be awaited or passed to "
            "asyncio.create_task(); decorate an `async def` batch function instead",
            RuntimeWarning,
            stacklevel=3,
        )

    def detach(batch: _SyncBatch) -> bool:
        """Close ``batch`` to new items. Call with ``lock`` held. False if already closed."""
        if open_batches.get(batch.group) is not batch:
            return False
        del open_batches[batch.group]
        counter.add(-len(batch.entries))
        return True

    def run(batch: _SyncBatch) -> None:
        keys = list(batch.entries)
        entries = list(batch.entries.values())
        try:
            result = func(*batch.prefix, [e.item for e in entries])
            outcomes = _split(name, result, keys)
        except BaseException as exc:
            for e in entries:
                e.future.set_exception(exc)
            if not isinstance(exc, Exception):
                raise
            return
        for e, (ok, value) in zip(entries, outcomes, strict=True):
            if ok:
                e.future.set_result(value)
            else:
                e.future.set_exception(value)

    @functools.wraps(func)
    def wrapper(*args: Any) -> Any:
        if not warned:
            warn_if_in_event_loop()
        prefix, item = _split_args(name, args)
        k = key_of(item)
        group = tuple(map(id, prefix))
        with lock:
            batch = open_batches.get(group)
            leader = batch is None
            if batch is None:
                batch = open_batches[group] = _SyncBatch(group, prefix)
            entry = batch.entries.get(k)
            if entry is None:
                entry = batch.entries[k] = _SyncEntry(item)
                counter.add(1)
            future = entry.future
            full = len(batch.entries) >= max_size and detach(batch)
        if full:
            run(batch)
        elif leader:
            # The first caller of a window keeps time for it: it waits up to max_wait for
            # its result (another thread may fill the batch and run it meanwhile), then
            # runs the batch itself. No timer thread is involved.
            done, _ = concurrent.futures.wait([future], timeout=max_wait)
            if not done:
                with lock:
                    mine = detach(batch)
                if mine:
                    run(batch)
        return future.result()

    def flush() -> None:
        with lock:
            batches = list(open_batches.values())
            for batch in batches:
                detach(batch)
        for batch in batches:
            run(batch)

    counter.wrapper = wrapper
    wrapper.pending = 0  # type: ignore[attr-defined]
    wrapper.flush = flush  # type: ignore[attr-defined]
    return wrapper


# --------------------------------------------------------------------------- async


class _AsyncEntry:
    __slots__ = ("future", "item", "waiters")

    def __init__(self, item: Any, loop: asyncio.AbstractEventLoop) -> None:
        self.item = item
        self.future: asyncio.Future[Any] = loop.create_future()
        self.waiters = 0


class _AsyncBatch:
    __slots__ = ("entries", "group", "prefix", "timer")

    def __init__(self, group: _GroupKey, prefix: tuple[Any, ...]) -> None:
        self.group = group
        self.prefix = prefix
        self.entries: dict[Hashable, _AsyncEntry] = {}
        self.timer: asyncio.Task[None] | None = None


class _LoopState:
    """Per-event-loop bookkeeping: open batches and batches being run."""

    def __init__(self) -> None:
        self.open: dict[_GroupKey, _AsyncBatch] = {}
        self.running: set[asyncio.Future[None]] = set()


def _consume(task: asyncio.Task[Any]) -> None:
    if not task.cancelled():
        task.exception()


def _batch_async(
    func: Callable[..., Awaitable[Any]],
    max_size: int,
    max_wait: float,
    key_of: Callable[[Any], Hashable],
) -> Callable[..., Any]:
    name = _core.qualname(func)
    loops: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _LoopState] = (
        weakref.WeakKeyDictionary()
    )
    counter = _Counter()

    async def run(batch: _AsyncBatch) -> None:
        keys = list(batch.entries)
        entries = list(batch.entries.values())
        try:
            result = await func(*batch.prefix, [e.item for e in entries])
            outcomes = _split(name, result, keys)
        except asyncio.CancelledError:
            for e in entries:
                e.future.cancel()
            raise
        except BaseException as exc:
            for e in entries:
                e.future.set_exception(exc)
                e.future.exception()  # mark retrieved: every waiter may be gone
            if not isinstance(exc, Exception):
                raise
            return
        # The entries' futures are still pending: callers that cancel only detach.
        for e, (ok, value) in zip(entries, outcomes, strict=True):
            if ok:
                e.future.set_result(value)
            else:
                e.future.set_exception(value)
                e.future.exception()

    def start(state: _LoopState, batch: _AsyncBatch) -> None:
        """Close ``batch`` and run it in its own task (shared by all its callers)."""
        if state.open.get(batch.group) is not batch:  # pragma: no cover - defensive
            return
        del state.open[batch.group]
        counter.add(-len(batch.entries))
        timer, batch.timer = batch.timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        task = asyncio.get_running_loop().create_task(run(batch))
        state.running.add(task)
        task.add_done_callback(state.running.discard)
        task.add_done_callback(_consume)

    async def run_inline(state: _LoopState, batch: _AsyncBatch, entry: _AsyncEntry) -> Any:
        """Close ``batch`` and run it in the calling task; return ``entry``'s result."""
        del state.open[batch.group]
        counter.add(-len(batch.entries))
        # Stands in for a batch task, so `aflush()` waits for this run too.
        running = asyncio.get_running_loop().create_future()
        state.running.add(running)
        try:
            with contextlib.suppress(Exception):  # delivered through entry.future
                await run(batch)
        finally:
            state.running.discard(running)
            running.set_result(None)
        return entry.future.result()

    async def wait_then_start(state: _LoopState, batch: _AsyncBatch) -> None:
        await asyncio.sleep(max_wait)  # a window, not a backoff: real time
        start(state, batch)

    @functools.wraps(func)
    async def wrapper(*args: Any) -> Any:
        prefix, item = _split_args(name, args)
        k = key_of(item)
        group = tuple(map(id, prefix))
        loop = asyncio.get_running_loop()
        state = loops.get(loop)
        if state is None:
            state = loops[loop] = _LoopState()
        batch = state.open.get(group)
        fresh = batch is None
        if batch is None:
            batch = state.open[group] = _AsyncBatch(group, prefix)
        entry = batch.entries.get(k)
        if entry is None:
            entry = batch.entries[k] = _AsyncEntry(item, loop)
            counter.add(1)
        entry.waiters += 1
        if len(batch.entries) >= max_size:
            if len(batch.entries) == 1 and entry.waiters == 1:
                # This caller is the batch's only waiter (e.g. max_size=1): run it right
                # here, in the calling task, with no extra task and no loop hops.
                # Cancelling this caller cancels only its own batch.
                return await run_inline(state, batch, entry)
            start(state, batch)
        elif fresh:
            batch.timer = loop.create_task(wait_then_start(state, batch))
        try:
            return await asyncio.shield(entry.future)
        except asyncio.CancelledError:
            entry.waiters -= 1
            if entry.waiters == 0 and state.open.get(group) is batch:
                # Nobody wants this item any more and it has not been sent: drop it.
                del batch.entries[k]
                counter.add(-1)
                entry.future.cancel()
                if not batch.entries:
                    del state.open[group]
                    if batch.timer is not None:
                        batch.timer.cancel()
            raise

    async def aflush() -> None:
        state = loops.get(asyncio.get_running_loop())
        if state is None:
            return
        for batch in list(state.open.values()):
            start(state, batch)
        if state.running:
            await asyncio.wait(list(state.running))

    counter.wrapper = wrapper
    wrapper.pending = 0  # type: ignore[attr-defined]
    wrapper.aflush = aflush  # type: ignore[attr-defined]
    return wrapper


# --------------------------------------------------------------------------- public


def batch_calls(
    *,
    max_size: int = 100,
    max_wait: float = 0.01,
    key: Callable[[Any], Hashable] | None = None,
) -> _BatchDecorator:
    """Coalesce concurrent single-item calls into one call of a batch function.

    Decorate a function that takes a **list of items** and returns their results. The
    decorated object is called with **one item** and returns that item's result. Calls
    made within `max_wait` seconds of the first call of a window — or until `max_size`
    distinct items are queued — are sent to the function together, as one list.

    **Output contract.** The batch function receives the distinct items in arrival order
    and must return either:

    * a **sequence** (list, tuple, ...; not `str`/`bytes`) with exactly one result per
      item, in the same order; or
    * a **mapping** from each item's key to its result. The key is `key(item)` when
      `key=` is given, otherwise the item itself. Extra entries are ignored.

    If the sequence has the wrong length, or the return value is neither a sequence nor a
    mapping, every caller in the batch gets a `BatchError`. If a mapping lacks a key, only
    the callers of that item get a `BatchError`; the others get their results. If the
    batch function raises, every caller in the batch gets that exception.

    **Deduplication.** Items with the same key in one window are sent once and their
    callers share the result. The key must be hashable.

    * `async def` batch functions: callers on one event loop `await loader(item)`. A
      window starts with the first call and closes after `max_wait`; the batch runs in its
      own task, so cancelling one caller does not cancel it for the others. A cancelled
      caller whose item has not been sent yet removes it from the batch (unless another
      caller is waiting for the same key). Batches never span event loops.
    * Sync batch functions: callers are threads, each blocking until its result is ready.
      The first caller of a window waits up to `max_wait` (or until the batch fills) and
      then runs the batch in its own thread; no timer thread is started. If a caller
      fills the batch to `max_size`, it runs the batch at once. Calling a sync loader
      sequentially from one thread therefore costs up to `max_wait` per call.
      A sync loader is **not** awaitable: `await loader(1)` or
      `asyncio.create_task(loader(1))` fails (the call returns a plain value, after
      blocking the event loop while it waits). Called from a running event loop, it
      emits a one-time `RuntimeWarning`; in async code decorate an `async def` batch
      function instead.
    * When a call fills an async batch to `max_size` and its caller is the batch's only
      waiter (always the case with `max_size=1`), the batch runs right in the calling
      task, without an extra task or event-loop round trip.

    On a method, the batch function is called as `func(self, items)`, and calls on
    different instances (or classes, for a class method) go to separate batches.

    The wrapper also has:

    * `.pending` — the number of distinct items queued and not yet sent.
    * `.flush()` (sync) — send every queued batch now, in the calling thread.
    * `await .aflush()` (async) — send the batches queued on the running loop now and
      wait until every batch running on that loop has finished.

    Args:
        max_size: Send a batch as soon as it holds this many distinct items (>= 1).
        max_wait: Seconds to wait for more items after the first call of a window (>= 0).
        key: Function of an item returning the hashable key used for deduplication and
            for looking results up in a mapping. Defaults to the item itself.

    Returns:
        A decorator producing a `Batched` or `AsyncBatched` wrapper.

    Raises:
        ValueError: If `max_size` < 1 or `max_wait` < 0.
        TypeError: When called, if the item's key is not hashable.
        BatchError: When called, if the batch function broke the output contract.

    Example:
        >>> @batch_calls(max_size=50, max_wait=0.005)
        ... async def load_users(ids: list[int]) -> dict[int, str]:
        ...     return {i: f"user{i}" for i in ids}
        >>> # await load_users(1), from many tasks at once -> one call with [1, ...]
    """
    if type(max_size) is not int or max_size < 1:
        raise ValueError(f"batch_calls max_size must be an integer >= 1, got {max_size!r}")
    if max_wait < 0:
        raise ValueError(f"batch_calls max_wait must be non-negative, got {max_wait!r}")
    user_key = key

    def key_of(item: Any) -> Hashable:
        k = item if user_key is None else user_key(item)
        try:
            hash(k)
        except TypeError:
            raise TypeError(
                f"batch_calls: key of type {type(k).__name__!r} is not hashable; "
                "pass key=<callable> returning a hashable key"
            ) from None
        return cast(Hashable, k)

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        if _core.is_async(func):
            return _batch_async(func, max_size, max_wait, key_of)
        return _batch_sync(func, max_size, max_wait, key_of)

    return cast(_BatchDecorator, decorate)
