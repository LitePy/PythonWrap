"""Bounded, explicit-lifecycle background writers for batches of independent records.

Records live in memory by default. Pass ``journal=`` (`SQLiteJournal`, or
`RedisStreamJournal`) to make accepted records durable: each one is written to the
journal before ``submit`` returns, removed once its batch has been written, and replayed
into the writer the next time it starts. Delivery is then at least once.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import contextvars
import functools
import json
import math
import os
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable, Coroutine, Hashable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast, overload, runtime_checkable

from python_wrap import _core, _fork
from python_wrap._runtime import seconds

__all__ = [
    "AsyncBuffered",
    "BufferJournal",
    "BufferStats",
    "Buffered",
    "RedisStreamJournal",
    "SQLiteJournal",
    "buffered",
]


@runtime_checkable
class BufferJournal(Protocol):
    """Durable storage for records a buffered writer has accepted but not yet written.

    * ``append(record)`` persists one record and returns its id. It must not return
      before the record is durable.
    * ``ack(ids)`` removes records whose batch was written (or that were dropped on
      purpose by ``on_full="drop_oldest"``).
    * ``pending()`` returns every remaining ``(id, record)``, oldest first; the writer
      replays them when it starts.

    One journal (one SQLite ``name``, one Redis stream) belongs to one live writer:
    two processes replaying the same journal would both write its records.
    """

    def append(self, record: Any, /) -> Hashable: ...

    def ack(self, ids: Sequence[Hashable], /) -> None: ...

    def pending(self) -> list[tuple[Hashable, Any]]: ...


def _json_dumps(record: Any) -> bytes:
    return json.dumps(record, separators=(",", ":")).encode("utf-8")


def _json_loads(data: bytes) -> Any:
    return json.loads(data)


_JOURNAL_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS python_wrap_buffer ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, payload BLOB NOT NULL)",
    "CREATE INDEX IF NOT EXISTS python_wrap_buffer_name ON python_wrap_buffer (name, id)",
)


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class SQLiteJournal:
    """A `BufferJournal` in a local SQLite file (stdlib only).

    Records are serialised with JSON by default, so they must be JSON-serialisable
    (tuples come back as lists); pass ``dumps``/``loads`` (for example pickle, for a
    file only you can write) for other types. Each ``append`` is its own committed
    transaction, so a record ``submit`` accepted survives a crash of the process.

    Args:
        path: Database file (created on first use). In-memory databases are rejected:
            they would not survive the restart the journal exists for.
        name: Journal name inside the file; several writers can share one file with
            different names.
        dumps: Record serialiser returning bytes.
        loads: Deserialiser matching ``dumps``.
        busy_timeout: Seconds to wait for a lock held by another connection.

    Raises:
        ValueError: For an empty or in-memory path, an empty name or a bad timeout.

    Example:
        ```python
        import os
        import tempfile

        from python_wrap.buffering import SQLiteJournal, buffered

        with tempfile.TemporaryDirectory() as tmp:
            journal = SQLiteJournal(os.path.join(tmp, "outbox.db"))
            written = []

            @buffered(max_size=10, flush_interval=0.01, journal=journal)
            def write(rows):
                written.extend(rows)

            assert write.submit({"id": 1})
            assert write.close(timeout=2)
            assert written == [{"id": 1}] and journal.pending() == []
            journal.close()
        ```
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        name: str = "default",
        dumps: Callable[[Any], bytes] = _json_dumps,
        loads: Callable[[bytes], Any] = _json_loads,
        busy_timeout: float = 5.0,
    ) -> None:
        text = os.fspath(path)
        if not text or text == ":memory:" or text.startswith("file::memory:"):
            raise ValueError("path must name a database file")
        if not name:
            raise ValueError("name must not be empty")
        if not math.isfinite(busy_timeout) or busy_timeout < 0:
            raise ValueError("busy_timeout must be finite and >= 0")
        self.path, self.name, self.busy_timeout = text, name, float(busy_timeout)
        self.dumps, self.loads = dumps, loads
        self._lock = _fork.ForkSafeLock()
        self._conn: sqlite3.Connection | None = None
        self._abandoned: list[sqlite3.Connection] = []
        self._pid = os.getpid()
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        # Never use or close a connection inherited across fork.
        if self._conn is not None:
            self._abandoned.append(self._conn)
        self._conn = None
        self._pid = os.getpid()

    def _connection(self) -> sqlite3.Connection:
        if self._pid != os.getpid():
            self._reset_after_fork()
        if self._conn is None:
            conn = sqlite3.connect(
                self.path, timeout=self.busy_timeout, isolation_level=None, check_same_thread=False
            )
            try:
                conn.execute(f"PRAGMA busy_timeout={int(self.busy_timeout * 1000)}")
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                for statement in _JOURNAL_SCHEMA:
                    conn.execute(statement)
            except BaseException:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    def append(self, record: Any, /) -> int:
        """Persist one record and return its row id."""
        payload = self.dumps(record)
        with self._lock:
            cursor = self._connection().execute(
                "INSERT INTO python_wrap_buffer (name, payload) VALUES (?, ?)",
                (self.name, payload),
            )
            return int(cursor.lastrowid or 0)

    def ack(self, ids: Sequence[Hashable], /) -> None:
        """Delete written (or deliberately dropped) records."""
        if not ids:
            return
        with self._lock:
            conn = self._connection()
            for chunk in _chunks(list(ids), 500):
                # Only "?" placeholders are interpolated; values are bound parameters.
                marks = ",".join("?" * len(chunk))
                sql = "DELETE FROM python_wrap_buffer WHERE name = ? AND id IN (" + marks + ")"
                conn.execute(sql, (self.name, *chunk))  # nosec B608

    def pending(self) -> list[tuple[Hashable, Any]]:
        """Every unwritten record, oldest first. Undecodable rows are logged and skipped."""
        with self._lock:
            rows = (
                self._connection()
                .execute(
                    "SELECT id, payload FROM python_wrap_buffer WHERE name = ? ORDER BY id",
                    (self.name,),
                )
                .fetchall()
            )
        found: list[tuple[Hashable, Any]] = []
        for row_id, payload in rows:
            try:
                found.append((int(row_id), self.loads(bytes(payload))))
            except Exception:
                _log_failure(_core.get_logger(None), "buffered: undecodable journal record")
        return found

    def close(self) -> None:
        """Close this process's connection; the file and its records stay."""
        with self._lock:
            if self._conn is not None and self._pid == os.getpid():
                self._conn.close()
            self._conn = None


class RedisStreamJournal:
    """A `BufferJournal` in a Redis stream (``XADD``/``XDEL``/``XRANGE``).

    python-wrap never imports redis: pass a synchronous ``redis.Redis``-compatible
    client. Durability is whatever the Redis server provides (configure AOF with
    ``appendfsync always`` or ``everysec`` if records must survive a Redis restart).
    Journal calls are synchronous; with `AsyncBuffered` they run on the event loop, so
    prefer `SQLiteJournal` (a local file) for async writers.

    Args:
        client: A synchronous Redis client.
        stream: Stream key, unique to one writer.
        dumps: Record serialiser returning bytes (JSON by default).
        loads: Deserialiser matching ``dumps``.

    Raises:
        ValueError: If ``stream`` is empty.
    """

    _FIELD = "r"

    def __init__(
        self,
        client: Any,
        stream: str,
        *,
        dumps: Callable[[Any], bytes] = _json_dumps,
        loads: Callable[[bytes], Any] = _json_loads,
    ) -> None:
        if not stream:
            raise ValueError("stream must not be empty")
        self.client, self.stream = client, stream
        self.dumps, self.loads = dumps, loads

    def append(self, record: Any, /) -> Hashable:
        """``XADD`` one record; return its stream id."""
        found: Hashable = self.client.xadd(self.stream, {self._FIELD: self.dumps(record)})
        return found

    def ack(self, ids: Sequence[Hashable], /) -> None:
        """``XDEL`` written (or deliberately dropped) records."""
        for chunk in _chunks(list(ids), 500):
            self.client.xdel(self.stream, *chunk)

    def pending(self) -> list[tuple[Hashable, Any]]:
        """Every record still in the stream, oldest first (read in pages of 500)."""
        found: list[tuple[Hashable, Any]] = []
        start: Any = "-"
        while True:
            page = self.client.xrange(self.stream, min=start, max="+", count=500)
            for entry_id, fields in page:
                payload = fields.get(self._FIELD, fields.get(self._FIELD.encode()))
                try:
                    found.append((entry_id, self.loads(payload)))
                except Exception:
                    _log_failure(_core.get_logger(None), "buffered: undecodable journal record")
            if len(page) < 500:
                return found
            last = page[-1][0]
            start = "(" + (last.decode() if isinstance(last, bytes) else str(last))


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

    With ``journal=``, every accepted record is persisted before ``submit`` returns
    and removed only after its batch was written. Records left in the journal (the
    process died, a batch failed, the writer was disabled or closed with a backlog) are
    replayed when the writer next starts: on the first ``submit`` or an explicit
    `replay`. Delivery is at least once, so handlers should be idempotent.
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
        journal: BufferJournal | None = None,
    ) -> None:
        _validate(max_size, max_queue, on_full, on_error)
        _validate_journal(journal)
        self.func, self.max_size, self.max_queue = func, max_size, max_queue
        self.interval = seconds(flush_interval)
        self.drain = seconds(drain_on_exit, positive=False)
        self.on_full, self.on_error = on_full, on_error
        self.log = _core.get_logger(logger)
        self.journal = journal
        self._replayed = journal is None
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
            "journal": journal,
        }
        functools.update_wrapper(self, func, updated=())
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        # A child never replays a journal on its own: the parent (or a sibling) may be
        # replaying the same records. Call replay() explicitly in the one process
        # that owns the journal.
        self._replayed = True
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
        """Queue an item, returning False when disabled, closed, full, or timed out.

        With a journal the record is persisted before this returns True; an error from
        the journal propagates and the record is not queued.
        """
        end = None if timeout is None else time.monotonic() + seconds(timeout, positive=False)
        with self._condition:
            if self._fork_dropped:
                _log_warning(
                    self.log,
                    "buffered: discarded %d inherited pending records after fork",
                    self._fork_dropped,
                )
                self._fork_dropped = 0
            self._replay_locked()
            while self._enabled and not self._closing and len(self._queue) >= self.max_queue:
                if self.on_full == "drop":
                    self._dropped += 1
                    return False
                if self.on_full == "drop_oldest":
                    self._forget([self._queue.popleft()])
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
            record_id = None if self.journal is None else self.journal.append(item)
            self._queue.append((record_id, item))
            self._pending += 1
            try:
                self._start_locked()
            except BaseException:
                self._queue.pop()
                self._pending -= 1
                raise
            self._condition.notify_all()
            return True

    def replay(self) -> int:
        """Load the journal's unwritten records into the queue and start the writer.

        Runs automatically on the first ``submit``; call it at startup to drain a
        backlog before anything new is submitted. Only the first call (automatic or
        explicit) replays; later calls return 0, as do writers without a journal.

        Returns:
            The number of records replayed.
        """
        with self._condition:
            count = self._replay_locked()
            if count:
                self._start_locked()
                self._condition.notify_all()
            return count

    def _replay_locked(self) -> int:
        if self._replayed or self.journal is None:
            return 0
        records = self.journal.pending()
        self._replayed = True
        self._queue.extendleft(reversed(records))
        self._pending += len(records)
        if records:
            _log_warning(self.log, "buffered: replaying %d journaled records", len(records))
        return len(records)

    def _start_locked(self) -> None:
        if self._thread is not None:
            return
        ctx = contextvars.copy_context()
        self._thread = threading.Thread(
            target=ctx.run, args=(self._run,), daemon=True, name="python-wrap-buffered"
        )
        try:
            self._thread.start()
        except BaseException:
            self._thread = None
            raise
        atexit.register(self.close, self.drain)

    def _forget(self, entries: Sequence[tuple[Hashable | None, Any]]) -> None:
        """Remove written or deliberately dropped records from the journal."""
        _ack(self.journal, entries, self.log)

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
                self.func([item for _, item in batch])
                self._forget(batch)
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

    ``journal=`` works as for `Buffered` (replayed on the first ``submit`` or an
    explicit ``await replay()``). Journal calls are synchronous and run on the event
    loop, so use a local `SQLiteJournal`.
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
        journal: BufferJournal | None = None,
    ) -> None:
        _validate(max_size, max_queue, on_full, on_error)
        _validate_journal(journal)
        self.func, self.max_size, self.max_queue = func, max_size, max_queue
        self.interval = seconds(flush_interval)
        self.drain = seconds(drain_on_exit, positive=False)
        self.on_full, self.on_error = on_full, on_error
        self.log = _core.get_logger(logger)
        self.journal = journal
        self._replayed = journal is None
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
            "journal": journal,
        }
        functools.update_wrapper(self, func, updated=())
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._replayed = True  # see Buffered._reset_after_fork
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
        self._replay_now()
        while self._enabled and not self._closing and len(self._queue) >= self.max_queue:
            if self.on_full == "drop":
                self._dropped += 1
                return False
            if self.on_full == "drop_oldest":
                _ack(self.journal, [self._queue.popleft()], self.log)
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
        record_id = None if self.journal is None else self.journal.append(item)
        self._queue.append((record_id, item))
        self._pending += 1
        try:
            self._start(loop)
        except BaseException:
            self._queue.pop()
            self._pending -= 1
            raise
        self._changed.set()
        return True

    async def replay(self) -> int:
        """`Buffered.replay` for async writers: load the journal and start the writer."""
        loop = self._check_loop()
        count = self._replay_now()
        if count:
            self._start(loop)
            self._changed.set()
        return count

    def _replay_now(self) -> int:
        if self._replayed or self.journal is None:
            return 0
        records = self.journal.pending()
        self._replayed = True
        self._queue.extendleft(reversed(records))
        self._pending += len(records)
        if records:
            _log_warning(self.log, "buffered: replaying %d journaled records", len(records))
        return len(records)

    def _start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._task is None:
            self._loop = loop
            self._task = loop.create_task(self._run())

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
                    await self.func([item for _, item in active])
                    _ack(self.journal, active, self.log)
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


def _ack(
    journal: BufferJournal | None, entries: Sequence[tuple[Hashable | None, Any]], log: Any
) -> None:
    """Acknowledge journaled entries; a failure only means they may be replayed again."""
    if journal is None:
        return
    ids = [record_id for record_id, _ in entries if record_id is not None]
    if not ids:
        return
    try:
        journal.ack(ids)
    except Exception:
        _log_failure(log, "buffered: journal ack failed; records may be written again")


def _validate_journal(journal: Any) -> None:
    if journal is not None and not isinstance(journal, BufferJournal):
        raise TypeError("journal must implement BufferJournal: append, ack and pending")


def _bound_writer(writer: Any, instance: Any) -> Any:
    """Keep independent, lazily started buffers on each instance/class."""
    if writer.journal is not None:
        raise TypeError(
            "buffered methods cannot use journal=: every instance would replay the same "
            "records; use a module-level buffered function"
        )
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
    journal: BufferJournal | None = None,
) -> _BufferedDecorator:
    """Decorate a batch handler with a lazy sync or async background buffer.

    ``journal`` (a `SQLiteJournal`, `RedisStreamJournal` or any `BufferJournal`) makes
    accepted records durable with at-least-once replay after a restart; without it,
    records live only in memory.
    """
    options: dict[str, Any] = {
        "max_size": max_size,
        "flush_interval": flush_interval,
        "max_queue": max_queue,
        "on_full": on_full,
        "drain_on_exit": drain_on_exit,
        "on_error": on_error,
        "logger": logger,
        "journal": journal,
    }

    def decorate(func: Callable[..., Any]) -> Any:
        return AsyncBuffered(func, **options) if _core.is_async(func) else Buffered(func, **options)

    return cast(_BufferedDecorator, decorate)
