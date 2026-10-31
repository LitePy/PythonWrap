"""Graceful shutdown: stop, flush and drain everything in one ordered pass.

Register the things that must be wound down (periodic tasks, buffered writers, batchers,
debouncers, bulkheads, or any callable), then call `shutdown.drain()` from your
framework's shutdown hook, or let `install_signal_handlers()` / `install_atexit()` do it.

* `ShutdownManager` — the registry and the drain logic; `shutdown` is the shared instance.
* `ShutdownReport` / `ItemReport` — what happened to each item.
* `ShutdownError` — raised by `drain(strict=True)` when something failed.

Draining never raises by default: a failing item is recorded and the rest still run.
"""

from __future__ import annotations

import asyncio
import atexit
import inspect
import signal
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, Literal, NoReturn

from python_wrap import _core, _fork
from python_wrap.adaptive import AdaptiveLimiter
from python_wrap.buffering import AsyncBuffered, Buffered
from python_wrap.bulkhead import Bulkhead
from python_wrap.errors import PythonWrapError
from python_wrap.periodic import AsyncPeriodic, Periodic

__all__ = ["ItemReport", "ShutdownError", "ShutdownManager", "ShutdownReport", "shutdown"]

Status = Literal["ok", "error", "timeout"]
_POLL = 0.01


class ShutdownError(PythonWrapError):
    """`drain(strict=True)` finished with at least one failed or timed-out item.

    Attributes:
        report: The full `ShutdownReport`.
    """

    def __init__(self, message: str, *, report: ShutdownReport) -> None:
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class ItemReport:
    """The outcome for one registered item.

    Attributes:
        name: The registered name.
        status: `"ok"`, `"error"` (it raised) or `"timeout"` (the budget ran out).
        error: The exception, for `"error"` (and a note for a skipped `"timeout"`).
        seconds: How long it took.
    """

    name: str
    status: Status
    error: BaseException | None = None
    seconds: float = 0.0


@dataclass(frozen=True)
class ShutdownReport:
    """What a drain did, in the order it ran.

    Attributes:
        items: One `ItemReport` per registered item.
        seconds: Total time spent.
    """

    items: tuple[ItemReport, ...] = ()
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        """True if every item finished cleanly."""
        return all(item.status == "ok" for item in self.items)

    @property
    def failed(self) -> list[ItemReport]:
        """Items that raised."""
        return [item for item in self.items if item.status == "error"]

    @property
    def timed_out(self) -> list[ItemReport]:
        """Items that did not finish within the budget."""
        return [item for item in self.items if item.status == "timeout"]

    def __str__(self) -> str:
        parts = ", ".join(f"{i.name}={i.status}" for i in self.items)
        return f"ShutdownReport({parts or 'empty'}; {self.seconds:.2f}s)"


Kind = Literal["periodic", "buffered", "idle", "async_only", "callable"]


@dataclass
class _Item:
    name: str
    priority: int
    seq: int
    target: Any
    kind: Kind
    fn: Callable[[], Any] | None = field(default=None)


def _classify(target: Any) -> tuple[Kind, Callable[[], Any] | None]:
    if isinstance(target, Periodic):
        return "periodic", None
    if isinstance(target, AsyncPeriodic):
        return "async_only", None
    if isinstance(target, Buffered):
        return "buffered", None
    if isinstance(target, AsyncBuffered):
        return "async_only", None
    if isinstance(target, (Bulkhead, AdaptiveLimiter)):
        return "idle", None
    aflush = getattr(target, "aflush", None)
    if callable(aflush):
        return "callable", aflush
    flush = getattr(target, "flush", None)
    if callable(flush):
        return "callable", flush
    if callable(target):
        return "callable", target
    raise TypeError(
        f"cannot register {target!r}: expected a python-wrap object (periodic, buffered, "
        "batcher, debouncer, bulkhead, limiter) or a callable"
    )


def _busy(target: Any) -> int:
    if isinstance(target, Bulkhead):
        return target.active + target.waiting
    return int(target.in_flight)


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


async def _await_value(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


def _call_in_thread(fn: Callable[[], Any], remaining: float | None) -> bool:
    """Run ``fn`` (awaiting an awaitable result) in a daemon thread; False on timeout."""
    outcome: list[BaseException] = []

    def target() -> None:
        try:
            result = fn()
            if inspect.isawaitable(result):
                asyncio.run(_await_value(result))
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=target, name="python-wrap-shutdown", daemon=True)
    thread.start()
    thread.join(remaining)
    if thread.is_alive():
        return False
    if outcome:
        raise outcome[0]
    return True


def _wait_idle_sync(target: Any, remaining: float | None) -> bool:
    deadline = None if remaining is None else time.monotonic() + remaining
    while _busy(target) > 0:
        left = _remaining(deadline)
        if left is not None and left <= 0:
            return False
        time.sleep(_POLL if left is None else min(_POLL, left))
    return True


async def _wait_idle_async(target: Any, remaining: float | None) -> bool:
    deadline = None if remaining is None else time.monotonic() + remaining
    while _busy(target) > 0:
        left = _remaining(deadline)
        if left is not None and left <= 0:
            return False
        await asyncio.sleep(_POLL if left is None else min(_POLL, left))
    return True


def _retrieve(task: asyncio.Future[Any]) -> None:
    if not task.cancelled():
        task.exception()


async def _within(awaitable: Awaitable[Any], remaining: float | None) -> tuple[bool, Any]:
    """Await with a real-time budget. Returns (finished, result); re-raises its error."""
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=remaining)
    if not done:
        task.cancel()
        task.add_done_callback(_retrieve)
        return False, None
    return True, task.result()


class ShutdownManager:
    """A registry of things to wind down, drained once and in order.

    `register` accepts python-wrap objects and plain callables:

    * `Periodic` / `AsyncPeriodic`: the loop is stopped (no new runs start);
    * `Buffered` / `AsyncBuffered`: closed, which flushes queued records first;
    * batchers (`batch_calls`) and debouncers (`debounce`): flushed;
    * `Bulkhead` / `AdaptiveLimiter`: waited on until no call is running or waiting;
    * any other callable (sync or `async def`): called.

    `drain` runs the items by ascending `priority` (ties in registration order) within a
    single time budget, records ok / error / timeout per item and never raises unless
    `strict=True`. Draining is idempotent: later calls return the first report. Register
    producers (periodic tasks) before consumers (buffers), e.g. periodic at `priority=0`,
    buffers at `10`, connection pools at `20`, and bulkheads at `5` so in-flight work
    finishes before writers close.

    Items that live on an event loop (`AsyncPeriodic`, `AsyncBuffered`) need `adrain()`;
    in a sync `drain()` they are reported as errors.

    A forked child starts with an empty registry (the parent's resources are not the
    child's to stop).

    Example:
        ```python
        from python_wrap.lifecycle import ShutdownManager

        manager = ShutdownManager()
        order: list[str] = []
        manager.register(lambda: order.append("close db"), name="db", priority=20)
        manager.register(lambda: order.append("stop jobs"), name="jobs", priority=0)

        report = manager.drain(timeout=5)
        assert report.ok and order == ["stop jobs", "close db"]
        assert manager.drain() is report      # second call is a no-op
        ```
    """

    def __init__(self) -> None:
        self._lock = _fork.ForkSafeLock()
        self._items: list[_Item] = []
        self._seq = 0
        self._state: Literal["idle", "draining", "done"] = "idle"
        self._report: ShutdownReport | None = None
        self._finished = threading.Event()
        self._drain_thread: int | None = None
        self._previous: dict[int, Any] = {}
        self._handlers: dict[int, Callable[[int, FrameType | None], None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._atexit_installed = False
        self._atexit_timeout: float | None = 30
        _fork.register(self)

    def __reduce__(self) -> NoReturn:
        _core.refuse_pickle(self)

    def _reset_after_fork(self) -> None:
        self._items = []
        self._seq = 0
        self._state = "idle"
        self._report = None
        self._finished = threading.Event()
        self._drain_thread = None
        self._tasks = set()

    # -- registry -----------------------------------------------------------------------

    def register(self, target: Any, *, name: str | None = None, priority: int = 0) -> Any:
        """Add something to wind down. Returns `target`, so it can wrap a constructor.

        Args:
            target: A python-wrap object or a callable (sync or async).
            name: Label for the report; defaults to the object's name.
            priority: Lower runs first.

        Returns:
            `target`, unchanged.

        Raises:
            TypeError: `target` is neither a known object nor callable.
            RuntimeError: Draining already started.
        """
        kind, fn = _classify(target)
        label = name or getattr(target, "_name", None) or getattr(target, "name", None)
        label = label if isinstance(label, str) and label else _core.qualname(target)
        with self._lock:
            if self._state != "idle":
                raise RuntimeError("cannot register: shutdown has already started")
            self._seq += 1
            self._items.append(_Item(label, priority, self._seq, target, kind, fn))
        return target

    def unregister(self, target: Any) -> bool:
        """Remove a registered object (by identity) or name. Returns True if found."""
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i.target is not target and i.name != target]
            return len(self._items) != before

    @property
    def names(self) -> list[str]:
        """Registered names in the order they would run."""
        with self._lock:
            return [i.name for i in self._ordered()]

    @property
    def shutting_down(self) -> bool:
        """True once a drain has started (use it for readiness checks)."""
        return self._state != "idle"

    @property
    def report(self) -> ShutdownReport | None:
        """The report of the finished drain, or `None`."""
        return self._report

    def reset(self) -> None:
        """Forget everything and allow a new drain. Meant for tests."""
        with self._lock:
            self._reset_after_fork()

    def _ordered(self) -> list[_Item]:
        return sorted(self._items, key=lambda i: (i.priority, i.seq))

    # -- drain --------------------------------------------------------------------------

    def _begin(self) -> list[_Item] | None:
        with self._lock:
            if self._state != "idle":
                return None
            self._state = "draining"
            self._drain_thread = threading.get_ident()
            items = self._ordered()
        _core.emit("lifecycle.drain_started", "shutdown", items=len(items))
        return items

    def _record(
        self, name: str, status: Status, error: BaseException | None, start: float
    ) -> ItemReport:
        seconds = time.monotonic() - start
        _core.emit("lifecycle.item_done", name, status=status, seconds=seconds)
        if status == "error":
            _core.get_logger().warning("shutdown: %s failed", name, exc_info=error)
        elif status == "timeout":
            _core.get_logger().warning("shutdown: %s did not finish in time", name)
        return ItemReport(name, status, error, seconds)

    def _complete(self, reports: list[ItemReport], start: float) -> ShutdownReport:
        report = ShutdownReport(tuple(reports), time.monotonic() - start)
        with self._lock:
            self._report = report
            self._state = "done"
            self._drain_thread = None
        self._finished.set()
        _core.emit("lifecycle.drain_finished", "shutdown", ok=report.ok, seconds=report.seconds)
        return report

    def _existing(self, timeout: float | None) -> ShutdownReport:
        if self._state == "draining" and self._drain_thread != threading.get_ident():
            self._finished.wait(timeout)
        return self._report or ShutdownReport()

    def _finish_strict(self, report: ShutdownReport, strict: bool) -> ShutdownReport:
        if strict and not report.ok:
            raise ShutdownError(f"shutdown incomplete: {report}", report=report)
        return report

    def drain(self, timeout: float | None = 30, *, strict: bool = False) -> ShutdownReport:
        """Wind everything down, blocking the calling thread.

        Args:
            timeout: Total seconds for the whole drain (real time); `None` waits forever.
                Items that would start after the budget is spent are skipped and
                reported as `"timeout"`.
            strict: Raise `ShutdownError` if any item failed or timed out.

        Returns:
            The `ShutdownReport`. A second call returns the first report unchanged.

        Raises:
            ShutdownError: `strict=True` and the drain was not clean.
        """
        items = self._begin()
        if items is None:
            return self._finish_strict(self._existing(timeout), strict)
        start = time.monotonic()
        deadline = None if timeout is None else start + timeout
        reports: list[ItemReport] = []
        for item in items:
            began = time.monotonic()
            left = _remaining(deadline)
            if left is not None and left <= 0:
                reports.append(self._record(item.name, "timeout", None, began))
                continue
            try:
                finished = self._run_sync(item, left)
            except Exception as exc:
                reports.append(self._record(item.name, "error", exc, began))
            else:
                reports.append(
                    self._record(item.name, "ok" if finished else "timeout", None, began)
                )
        return self._finish_strict(self._complete(reports, start), strict)

    def _run_sync(self, item: _Item, remaining: float | None) -> bool:
        target = item.target
        if item.kind == "periodic":
            return bool(target.stop(remaining))
        if item.kind == "buffered":
            return bool(target.close(remaining))
        if item.kind == "idle":
            return _wait_idle_sync(target, remaining)
        if item.kind == "async_only":
            raise RuntimeError(f"{item.name} runs on an event loop: use adrain()")
        assert item.fn is not None
        return _call_in_thread(item.fn, remaining)

    async def adrain(self, timeout: float | None = 30, *, strict: bool = False) -> ShutdownReport:
        """Async variant of `drain`: runs on the event loop, blocking work in threads.

        Same semantics, ordering and idempotence as `drain`. Use it in async framework
        hooks (FastAPI lifespan, aiohttp `on_cleanup`).
        """
        items = self._begin()
        if items is None:
            deadline = None if timeout is None else time.monotonic() + timeout
            while self._state == "draining" and (left := _remaining(deadline)) != 0:
                await asyncio.sleep(_POLL if left is None else min(_POLL, left))
            return self._finish_strict(self._report or ShutdownReport(), strict)
        start = time.monotonic()
        deadline = None if timeout is None else start + timeout
        reports: list[ItemReport] = []
        for item in items:
            began = time.monotonic()
            left = _remaining(deadline)
            if left is not None and left <= 0:
                reports.append(self._record(item.name, "timeout", None, began))
                continue
            try:
                finished = await self._run_async(item, left)
            except Exception as exc:
                reports.append(self._record(item.name, "error", exc, began))
            else:
                reports.append(
                    self._record(item.name, "ok" if finished else "timeout", None, began)
                )
        return self._finish_strict(self._complete(reports, start), strict)

    async def _run_async(self, item: _Item, remaining: float | None) -> bool:
        target = item.target
        if item.kind == "periodic":
            return bool(await asyncio.to_thread(target.stop, remaining))
        if item.kind == "async_only" and isinstance(target, AsyncPeriodic):
            return (await _within(target.stop(), remaining))[0]
        if item.kind == "buffered":
            return bool(await asyncio.to_thread(target.close, remaining))
        if item.kind == "async_only":
            finished, result = await _within(target.close(remaining), remaining)
            return finished and bool(result)
        if item.kind == "idle":
            return await _wait_idle_async(target, remaining)
        fn = item.fn
        assert fn is not None
        deadline = None if remaining is None else time.monotonic() + remaining
        if _core.is_async(fn):
            return (await _within(fn(), remaining))[0]
        finished, result = await _within(asyncio.to_thread(fn), remaining)
        if finished and inspect.isawaitable(result):
            finished, _ = await _within(result, _remaining(deadline))
        return finished

    # -- atexit -------------------------------------------------------------------------

    def install_atexit(self, timeout: float | None = 30) -> None:
        """Drain when the interpreter exits normally. Safe to call repeatedly."""
        with self._lock:
            if self._atexit_installed:
                return
            self._atexit_installed = True
        self._atexit_timeout = timeout
        atexit.register(self._on_atexit)

    def uninstall_atexit(self) -> None:
        """Undo `install_atexit`."""
        with self._lock:
            if not self._atexit_installed:
                return
            self._atexit_installed = False
        atexit.unregister(self._on_atexit)

    def _on_atexit(self) -> None:
        try:
            self.drain(self._atexit_timeout)
        except Exception:  # pragma: no cover - drain does not raise unless strict
            _core.get_logger().exception("shutdown at exit failed")

    # -- signals ------------------------------------------------------------------------

    def install_signal_handlers(
        self,
        signals: Iterable[int] = (signal.SIGTERM, signal.SIGINT),
        *,
        timeout: float | None = 30,
        exit: bool = True,
        chain: bool = True,
    ) -> list[int]:
        """Drain when one of `signals` arrives.

        Inside a running event loop the handler schedules `adrain()` on it; otherwise it
        runs `drain()` right in the handler. Afterwards the previous handler is called
        (`chain`), and the process exits with status `128 + signum` (`exit`) unless the
        previous handler already ended it (the default `SIGINT` handler raises
        `KeyboardInterrupt`). A second signal while draining skips straight to the
        exit step, so a second Ctrl-C forces the exit.

        Args:
            signals: Signal numbers to handle.
            timeout: Drain budget in seconds.
            exit: Exit the process after draining.
            chain: Call the handler that was installed before.

        Returns:
            The signals that were installed (already-installed ones are skipped).

        Raises:
            RuntimeError: Not called from the main thread.
        """
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("signal handlers can only be installed from the main thread")
        installed: list[int] = []
        for signum in signals:
            signum = int(signum)
            if signum in self._handlers:
                continue

            def handler(
                received: int,
                frame: FrameType | None,
                _t: float | None = timeout,
                _e: bool = exit,
                _c: bool = chain,
            ) -> None:
                self._on_signal(received, frame, _t, _e, _c)

            self._previous[signum] = signal.signal(signum, handler)
            self._handlers[signum] = handler
            installed.append(signum)
        return installed

    def restore_signal_handlers(self) -> list[int]:
        """Put back the handlers that were replaced. Returns the signals restored.

        A handler someone installed on top of ours is left alone.
        """
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("signal handlers can only be restored from the main thread")
        restored: list[int] = []
        for signum, ours in list(self._handlers.items()):
            if signal.getsignal(signum) is ours:
                previous = self._previous.get(signum)
                signal.signal(signum, signal.SIG_DFL if previous is None else previous)
                restored.append(signum)
            del self._handlers[signum]
            self._previous.pop(signum, None)
        return restored

    def _on_signal(
        self, signum: int, frame: FrameType | None, timeout: float | None, exit: bool, chain: bool
    ) -> None:
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            task = loop.create_task(self._signal_async(signum, frame, timeout, exit, chain))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return
        self.drain(timeout)
        self._after_signal(signum, frame, exit, chain)

    async def _signal_async(
        self, signum: int, frame: FrameType | None, timeout: float | None, exit: bool, chain: bool
    ) -> None:
        await self.adrain(timeout)
        self._after_signal(signum, frame, exit, chain)

    def _after_signal(self, signum: int, frame: FrameType | None, exit: bool, chain: bool) -> None:
        previous = self._previous.get(signum)
        if chain and callable(previous):
            previous(signum, frame)
        if exit:
            sys.exit(128 + signum)


shutdown = ShutdownManager()
"""The shared process-wide manager."""
