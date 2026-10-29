"""Bounded, explicit-lifecycle background writers for batches of independent records."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import contextvars
import functools
import threading
import time
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Protocol, cast, overload

from python_wrap import _core, _fork
from python_wrap._runtime import seconds

__all__ = ["AsyncBuffered", "BufferStats", "Buffered", "buffered"]


@dataclass(frozen=True)
class BufferStats:
    """Pending includes records currently being written; failures are counted separately."""

    queued: int
    written: int
    dropped: int
    failed_batches: int
    failed_records: int
    enabled: bool


class Buffered:
    """A synchronous batch callable with submit/flush/close/disable/stats controls.

    A writer thread starts lazily on first submission. Queued records are discarded
    after fork to avoid duplicate writes; each child may start its own writer.
    Failed batches are counted and logged, not retried (use retry on the handler).
    """

    def __init__(
        self,
        func: Callable[[list[Any]], Any],
        *,
        max_size: int = 200,
        flush_interval: float = 1,
        max_queue: int = 10000,
        on_full: str = "drop",
        drain_on_exit: float = 3,
        on_error: str = "continue",
        logger: Any = None,
    ) -> None:
        _validate(max_size, max_queue, on_full, on_error)
        self.func, self.max_size, self.max_queue = func, max_size, max_queue
        self.interval = seconds(flush_interval)
        self.drain = seconds(drain_on_exit, positive=False)
        self.on_full, self.on_error = on_full, on_error
        self.log = _core.get_logger(logger)
        self._condition = threading.Condition()
        self._bindings_lock = _fork.ForkSafeLock()
        self._queue: deque[Any] = deque()
        self._pending = self._written = self._dropped = self._failed = self._failed_records = 0
        self._enabled = True
        self._closing = False
        self._flushing = 0
        self._thread: threading.Thread | None = None
        self._fork_dropped = 0
        self._options = {
            "max_size": max_size,
            "flush_interval": flush_interval,
            "max_queue": max_queue,
            "on_full": on_full,
            "drain_on_exit": drain_on_exit,
            "on_error": on_error,
            "logger": logger,
        }
        functools.update_wrapper(self, func, updated=())
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._condition = threading.Condition()
        self._fork_dropped = self._pending
        self._queue.clear()
        self._pending = 0
        self._thread = None
        self._flushing = 0

    def __get__(self, instance: Any, owner: type[Any] | None = None) -> Buffered:
        if instance is None:
            return self
        return cast(Buffered, _bound_writer(self, instance))

    def __call__(self, batch: list[Any]) -> Any:
        return self.func(batch)

    def submit(self, item: Any, *, timeout: float | None = None) -> bool:
        """Queue an item, returning False when disabled, closed, full, or timed out."""
        end = None if timeout is None else time.monotonic() + seconds(timeout, positive=False)
        with self._condition:
            if self._fork_dropped:
                _log_warning(
                    self.log,
                    "buffered: discarded %d inherited pending records after fork",
                    self._fork_dropped,
                )
                self._fork_dropped = 0
            while self._enabled and not self._closing and len(self._queue) >= self.max_queue:
                if self.on_full == "drop":
                    self._dropped += 1
                    return False
                if self.on_full == "drop_oldest":
                    self._queue.popleft()
                    self._pending -= 1
                    self._dropped += 1
                    break
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    self._dropped += 1
                    return False
                self._condition.wait(left)
            if not self._enabled or self._closing:
                self._dropped += 1
                return False
            self._queue.append(item)
            self._pending += 1
            if self._thread is None:
                ctx = contextvars.copy_context()
                self._thread = threading.Thread(
                    target=ctx.run, args=(self._run,), daemon=True, name="python-wrap-buffered"
                )
                try:
                    self._thread.start()
                except BaseException:
                    self._thread = None
                    self._queue.pop()
                    self._pending -= 1
                    raise
                atexit.register(self.close, self.drain)
            self._condition.notify_all()
            return True

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._closing:
                    self._condition.wait()
                if not self._queue:
                    return
                end = time.monotonic() + self.interval
                while len(self._queue) < self.max_size and not self._closing and not self._flushing:
                    left = end - time.monotonic()
                    if left <= 0:
                        break
                    self._condition.wait(left)
                batch = [self._queue.popleft() for _ in range(min(self.max_size, len(self._queue)))]
                self._condition.notify_all()
            failed = False
            try:
                self.func(batch)
            except BaseException as exc:
                failed = True
                _log_failure(self.log, "buffered: batch write failed")
                if not isinstance(exc, Exception):
                    with self._condition:
                        self._closing = True
                        self._enabled = False
                        self._dropped += len(self._queue)
                        self._pending -= len(self._queue)
                        self._queue.clear()
            finally:
                with self._condition:
                    self._pending -= len(batch)
                    if failed:
                        self._failed += 1
                        self._failed_records += len(batch)
                        if self.on_error == "disable":
                            self._enabled = False
                            self._dropped += len(self._queue)
                            self._pending -= len(self._queue)
                            self._queue.clear()
                    else:
                        self._written += len(batch)
                    self._condition.notify_all()

    def flush(self, timeout: float | None = 5) -> bool:
        """Wait for pending writes; return False on timeout. Does not stop the writer."""
        if self._thread is threading.current_thread():
            raise RuntimeError("a buffered handler cannot flush itself")
        end = None if timeout is None else time.monotonic() + seconds(timeout, positive=False)
        with self._condition:
            self._flushing += 1
            self._condition.notify_all()
            try:
                while self._pending:
                    left = None if end is None else end - time.monotonic()
                    if left is not None and left <= 0:
                        return False
                    self._condition.wait(left)
                return True
            finally:
                self._flushing -= 1

    def close(self, timeout: float | None = 5) -> bool:
        """Stop accepting, drain queued writes, then end the worker when it is idle."""
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        end = None if timeout is None else time.monotonic() + seconds(timeout, positive=False)
        done = self.flush(timeout)
        if done and self._thread is not None:
            left = None if end is None else max(0, end - time.monotonic())
            self._thread.join(left)
            done = not self._thread.is_alive()
        if done:
            atexit.unregister(self.close)
        return done

    def disable(self, reason: str = "disabled") -> None:
        """Stop accepting new records; already queued records still drain."""
        with self._condition:
            self._enabled = False
            self._condition.notify_all()
        _log_warning(self.log, "buffered disabled: %s", reason)

    def stats(self) -> BufferStats:
        with self._condition:
            return BufferStats(
                self._pending,
                self._written,
                self._dropped,
                self._failed,
                self._failed_records,
                self._enabled and not self._closing,
            )

    def __enter__(self) -> Buffered:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close(self.drain)


class _AsyncSignal:
    """Register waiters synchronously, before wait_for can schedule another task.

    Python 3.10 wait_for wraps coroutine arguments in a task. Sharing/clearing an
    Event between producers and flushers can lose its notification before that task
    starts. State predicates plus eagerly registered futures avoid that race.
    """

    def __init__(self) -> None:
        self.waiters: set[asyncio.Future[None]] = set()

    def wait(self) -> asyncio.Future[None]:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiters.add(future)
        future.add_done_callback(self.waiters.discard)
        return future

    def set(self) -> None:
        for future in tuple(self.waiters):
            if not future.done():
                future.set_result(None)
        self.waiters.clear()

    def clear(self) -> None:
        # Notifications have no sticky state: callers always recheck their predicate.
        pass


class AsyncBuffered:
    """Event-loop-local async writer with awaitable submit/flush/close controls.

    Use `async with writer` or explicitly await close before stopping its loop.
    Cancellation of close/flush does not cancel the writer. If the writer itself is
    cancelled, its active batch is counted as failed, queued records are dropped,
    and subsequent submissions are refused. Handlers may have performed side effects
    before cancellation, so failed batches are never retried automatically.
    """

    def __init__(
        self,
        func: Callable[[list[Any]], Any],
        *,
        max_size: int = 200,
        flush_interval: float = 1,
        max_queue: int = 10000,
        on_full: str = "drop",
        drain_on_exit: float = 3,
        on_error: str = "continue",
        logger: Any = None,
    ) -> None:
        _validate(max_size, max_queue, on_full, on_error)
        self.func, self.max_size, self.max_queue = func, max_size, max_queue
        self.interval = seconds(flush_interval)
        self.drain = seconds(drain_on_exit, positive=False)
        self.on_full, self.on_error = on_full, on_error
        self.log = _core.get_logger(logger)
        self._bindings_lock = _fork.ForkSafeLock()
        self._queue: deque[Any] = deque()
        self._changed = _AsyncSignal()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._written = self._dropped = self._failed = self._failed_records = 0
        self._pending = self._flushers = 0
        self._enabled, self._closing = True, False
        self._options = {
            "max_size": max_size,
            "flush_interval": flush_interval,
            "max_queue": max_queue,
            "on_full": on_full,
            "drain_on_exit": drain_on_exit,
            "on_error": on_error,
            "logger": logger,
        }
        functools.update_wrapper(self, func, updated=())
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._dropped += self._pending
        self._queue.clear()
        self._changed = _AsyncSignal()
        self._loop = None
        self._task = None
        self._pending = self._flushers = 0

    def __get__(self, instance: Any, owner: type[Any] | None = None) -> AsyncBuffered:
        if instance is None:
            return self
        return cast(AsyncBuffered, _bound_writer(self, instance))

    async def __call__(self, batch: list[Any]) -> Any:
        return await self.func(batch)

    def _check_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is not None and loop is not self._loop:
            raise RuntimeError("AsyncBuffered belongs to another event loop")
        return loop

    async def submit(self, item: Any, *, timeout: float | None = None) -> bool:
        """Queue one record; blocked calls recheck close/disable before acceptance."""
        loop = self._check_loop()
        end = None if timeout is None else loop.time() + seconds(timeout, positive=False)
        while self._enabled and not self._closing and len(self._queue) >= self.max_queue:
            if self.on_full == "drop":
                self._dropped += 1
                return False
            if self.on_full == "drop_oldest":
                self._queue.popleft()
                self._pending -= 1
                self._dropped += 1
                break
            self._changed.clear()
            left = None if end is None else end - loop.time()
            if left is not None and left <= 0:
                self._dropped += 1
                return False
            try:
                await asyncio.wait_for(self._changed.wait(), left)
            except asyncio.TimeoutError:
                self._dropped += 1
                return False
        if not self._enabled or self._closing:
            self._dropped += 1
            return False
        self._queue.append(item)
        self._pending += 1
        if self._task is None:
            self._loop = loop
            try:
                self._task = loop.create_task(self._run())
            except BaseException:
                self._queue.pop()
                self._pending -= 1
                raise
        self._changed.set()
        return True

    async def _run(self) -> None:
        active: list[Any] = []
        try:
            while True:
                while not self._queue and not self._closing:
                    self._changed.clear()
                    await self._changed.wait()
                if not self._queue:
                    return
                end = asyncio.get_running_loop().time() + self.interval
                while len(self._queue) < self.max_size and not self._closing and not self._flushers:
                    left = end - asyncio.get_running_loop().time()
                    if left <= 0:
                        break
                    self._changed.clear()
                    try:
                        await asyncio.wait_for(self._changed.wait(), left)
                    except asyncio.TimeoutError:
                        break
                active = [
                    self._queue.popleft() for _ in range(min(self.max_size, len(self._queue)))
                ]
                self._changed.set()
                try:
                    await self.func(active)
                except Exception:
                    self._failed += 1
                    self._failed_records += len(active)
                    _log_failure(self.log, "buffered: async batch write failed")
                    if self.on_error == "disable":
                        self._enabled = False
                        self._drop_queue()
                else:
                    self._written += len(active)
                self._pending -= len(active)
                active = []
                self._changed.set()
        finally:
            if active:
                self._failed += 1
                self._failed_records += len(active)
                self._pending -= len(active)
            self._enabled = False
            self._closing = True
            self._drop_queue()
            self._changed.set()

    def _drop_queue(self) -> None:
        self._dropped += len(self._queue)
        self._pending -= len(self._queue)
        self._queue.clear()

    async def flush(self, timeout: float | None = 5) -> bool:
        """Immediately dispatch queued batches and wait for writes, bounded by timeout."""
        loop = self._check_loop()
        if asyncio.current_task() is self._task:
            raise RuntimeError("a buffered handler cannot flush itself")
        end = None if timeout is None else loop.time() + seconds(timeout, positive=False)
        self._flushers += 1
        self._changed.set()
        try:
            while self._pending:
                left = None if end is None else end - loop.time()
                if left is not None and left <= 0:
                    return False
                self._changed.clear()
                try:
                    await asyncio.wait_for(self._changed.wait(), left)
                except asyncio.TimeoutError:
                    return False
            return True
        finally:
            self._flushers -= 1

    async def close(self, timeout: float | None = 5) -> bool:
        """Refuse new submissions and drain; cancellation leaves the writer running."""
        self._check_loop()
        self._closing = True
        self._changed.set()
        done = await self.flush(timeout)
        if done and self._task is not None and not self._task.cancelled():
            await asyncio.shield(self._task)
        return done

    def disable(self, reason: str = "disabled") -> None:
        self._enabled = False
        self._changed.set()
        _log_warning(self.log, "buffered disabled: %s", reason)

    def stats(self) -> BufferStats:
        return BufferStats(
            self._pending,
            self._written,
            self._dropped,
            self._failed,
            self._failed_records,
            self._enabled and not self._closing,
        )

    async def __aenter__(self) -> AsyncBuffered:
        self._check_loop()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close(self.drain)


def _bound_writer(writer: Any, instance: Any) -> Any:
    """Keep independent, lazily started buffers on each instance/class."""
    name = f"_python_wrap_buffer_{id(writer)}"
    with writer._bindings_lock:
        try:
            stored = vars(instance).get(name)
        except TypeError:
            raise TypeError("buffered methods require instances with __dict__") from None
        if stored is None:
            stored = type(writer)(functools.partial(writer.func, instance), **writer._options)
            setattr(instance, name, stored)
        return stored


def _log_failure(logger: Any, message: str) -> None:
    with contextlib.suppress(Exception):
        logger.exception(message)


def _log_warning(logger: Any, message: str, *args: Any) -> None:
    with contextlib.suppress(Exception):
        logger.warning(message, *args)


def _validate(size: int, capacity: int, on_full: str, on_error: str) -> None:
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or isinstance(capacity, bool)
        or not isinstance(capacity, int)
        or size < 1
        or capacity < 1
    ):
        raise ValueError("max_size and max_queue must be positive")
    if on_full not in {"drop", "block", "drop_oldest"}:
        raise ValueError("on_full must be drop, block or drop_oldest")
    if on_error not in {"continue", "disable"}:
        raise ValueError("on_error must be continue or disable")


class _BufferedDecorator(Protocol):
    @overload
    def __call__(self, func: Callable[..., Coroutine[Any, Any, Any]]) -> AsyncBuffered: ...
    @overload
    def __call__(self, func: Callable[..., Any]) -> Buffered: ...


def buffered(
    *,
    max_size: int = 200,
    flush_interval: float = 1,
    max_queue: int = 10000,
    on_full: str = "drop",
    drain_on_exit: float = 3,
    on_error: str = "continue",
    logger: Any = None,
) -> _BufferedDecorator:
    """Decorate a batch handler with a lazy sync or async background buffer."""
    options: dict[str, Any] = {
        "max_size": max_size,
        "flush_interval": flush_interval,
        "max_queue": max_queue,
        "on_full": on_full,
        "drain_on_exit": drain_on_exit,
        "on_error": on_error,
        "logger": logger,
    }

    def decorate(func: Callable[..., Any]) -> Any:
        return AsyncBuffered(func, **options) if _core.is_async(func) else Buffered(func, **options)

    return cast(_BufferedDecorator, decorate)
