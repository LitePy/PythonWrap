"""Result retention guarded by renewable owner-checked leases."""

from __future__ import annotations

import asyncio
import functools
import importlib
import json
import re
import uuid
from collections.abc import Callable
from contextlib import AsyncExitStack, ExitStack
from typing import Any, TypeVar, cast

from python_wrap import _core, _fork
from python_wrap._runtime import call_key, digest, seconds
from python_wrap.coordination import AlreadyRunning, Lease, LocalLease, _drain

__all__ = ["MemoryResultStore", "SqlResultStore", "idempotent"]
F = TypeVar("F", bound=Callable[..., Any])


class MemoryResultStore:
    """A thread-safe, bounded, expiring store for local idempotency results.

    Expired entries are swept as new ones arrive, and at most ``max_entries`` are kept:
    when full, the entry closest to expiry is evicted first. A key whose result was
    evicted simply runs again, so the bound trades a weaker guarantee under a flood of
    distinct keys for memory that stays bounded (each key may come from an untrusted
    client, for example through `python_wrap.web.IdempotencyMiddleware`).

    Args:
        max_entries: Most results kept at once (>= 1).

    Raises:
        ValueError: If ``max_entries`` is not a positive integer.

    Example:
        >>> store = MemoryResultStore(max_entries=2)
        >>> for key in ("a", "b", "c"):
        ...     store.set(key, key.upper(), 60)
        >>> len(store), store.get("a"), store.get("c")
        (2, None, 'C')
    """

    def __init__(self, max_entries: int = 10_000) -> None:
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        self.max_entries = max_entries
        self._data: dict[str, tuple[Any, float]] = {}
        self._lock = _fork.ForkSafeLock()
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def get(self, key: str) -> Any:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry[1] <= _core.monotonic():
                del self._data[key]
                return None
            return entry[0]

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            now = _core.monotonic()
            self._data.pop(key, None)
            if len(self._data) >= self.max_entries:
                for old in [k for k, (_, expires) in self._data.items() if expires <= now]:
                    del self._data[old]
            while len(self._data) >= self.max_entries:
                del self._data[min(self._data, key=lambda k: self._data[k][1])]
            self._data[key] = (value, now + ttl)

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")
_TYPES = {
    "postgresql": ("TEXT", "BIGINT"),
    "mssql": ("NVARCHAR(MAX)", "BIGINT"),
    "sqlite": ("TEXT", "INTEGER"),
}


class SqlResultStore:
    """Idempotency results in a table of the application's own database (SQLAlchemy).

    Rows hold ``idem_key`` (a SHA-256 of the call key), ``status`` (``running`` or
    ``done``), ``owner``, ``result`` (JSON text by default) and ``expires_at`` (Unix
    milliseconds). The same SQL runs on PostgreSQL, SQL Server and SQLite. The table is
    never created for you: run the statements from `SqlResultStore.ddl` in a migration.
    Call `purge_expired` from a scheduled job to drop old rows.

    As a plain ``store=`` it records results like any other store (pair it with a shared
    lease backend such as `SqlAppLock` or `PgAdvisoryLock` across processes). With
    ``idempotent(store=..., connection=...)`` the key row is written on the caller's own
    connection, so the key and the function's database writes commit or roll back
    together; the table's primary key turns a concurrent duplicate into a wait or an
    error instead of a second run.

    Args:
        engine: A SQLAlchemy ``Engine``.
        table: Table name (letters, digits and underscores only).
        dumps: Serializer for results; defaults to ``json.dumps``.
        loads: Deserializer matching ``dumps``.

    Raises:
        ValueError: If ``table`` is not a plain identifier.
    """

    fail_open = False

    def __init__(
        self,
        engine: Any,
        table: str = "pw_idempotency",
        *,
        dumps: Callable[[Any], str] = json.dumps,
        loads: Callable[[str], Any] = json.loads,
    ) -> None:
        if not isinstance(table, str) or not _IDENTIFIER.fullmatch(table):
            raise ValueError("table must be a plain SQL identifier")
        self.engine, self.table, self.dumps, self.loads = engine, table, dumps, loads

    @staticmethod
    def ddl(dialect: str, table: str = "pw_idempotency") -> list[str]:
        """``CREATE`` statements for ``dialect`` (``postgresql``, ``mssql`` or ``sqlite``)."""
        if dialect not in _TYPES:
            raise ValueError(f"dialect must be one of {sorted(_TYPES)}")
        if not _IDENTIFIER.fullmatch(table):
            raise ValueError("table must be a plain SQL identifier")
        text_type, int_type = _TYPES[dialect]
        return [
            f"CREATE TABLE {table} (idem_key VARCHAR(64) NOT NULL PRIMARY KEY,"
            f" status VARCHAR(8) NOT NULL, owner VARCHAR(64) NOT NULL,"
            f" result {text_type} NULL, expires_at {int_type} NOT NULL)",
            f"CREATE INDEX {table}_expires ON {table} (expires_at)",
        ]

    @staticmethod
    def _sql(statement: str) -> Any:
        return importlib.import_module("sqlalchemy").text(statement)

    @staticmethod
    def _duplicate() -> Any:
        """SQLAlchemy's IntegrityError (raised for a primary-key clash)."""
        return importlib.import_module("sqlalchemy.exc").IntegrityError

    @staticmethod
    def _now() -> int:
        return int(_core.wall_time() * 1000)

    def _read(self, conn: Any, key: str) -> tuple[Any] | None:
        # The table name was validated as an identifier; values are bound parameters.
        row = conn.execute(
            self._sql(f"SELECT status, result, expires_at FROM {self.table} WHERE idem_key=:k"),  # nosec B608
            {"k": digest(key)},
        ).first()
        if row is None or row[0] != "done" or int(row[2]) <= self._now():
            return None
        return (self.loads(row[1]),)

    def _claim(self, conn: Any, key: str, owner: str, ttl: float) -> bool:
        """Insert a ``running`` row, or take over an expired one. False if it is live."""
        params = {"k": digest(key), "o": owner, "e": self._now() + int(ttl * 1000)}
        try:
            with conn.begin_nested():
                conn.execute(
                    self._sql(
                        f"INSERT INTO {self.table} (idem_key, status, owner, result, expires_at)"  # nosec B608
                        " VALUES (:k, 'running', :o, NULL, :e)"
                    ),
                    params,
                )
            return True
        except self._duplicate():
            pass
        taken = conn.execute(
            self._sql(
                f"UPDATE {self.table} SET status='running', owner=:o, result=NULL,"  # nosec B608
                " expires_at=:e WHERE idem_key=:k AND expires_at<=:now"
            ),
            {**params, "now": self._now()},
        )
        return bool(taken.rowcount == 1)

    def _finish(self, conn: Any, key: str, value: Any, ttl: float, owner: str) -> None:
        conn.execute(
            self._sql(
                f"UPDATE {self.table} SET status='done', result=:r, expires_at=:e"  # nosec B608
                " WHERE idem_key=:k AND owner=:o"
            ),
            {
                "k": digest(key),
                "o": owner,
                "r": self.dumps(value),
                "e": self._now() + int(ttl * 1000),
            },
        )

    # -- the result-store contract used by idempotent ------------------------------------
    def get(self, key: str) -> tuple[Any] | None:
        with self.engine.connect() as conn:
            return self._read(conn, key)

    def set(self, key: str, entry: tuple[Any], ttl: float) -> None:
        params = {
            "k": digest(key),
            "o": "",
            "r": self.dumps(entry[0]),
            "e": self._now() + int(ttl * 1000),
        }
        update = self._sql(
            f"UPDATE {self.table} SET status='done', owner=:o, result=:r, expires_at=:e"  # nosec B608
            " WHERE idem_key=:k"
        )
        with self.engine.begin() as conn:
            if conn.execute(update, params).rowcount:
                return
            try:
                with conn.begin_nested():
                    conn.execute(
                        self._sql(
                            f"INSERT INTO {self.table}"  # nosec B608
                            " (idem_key, status, owner, result, expires_at)"
                            " VALUES (:k, 'done', :o, :r, :e)"
                        ),
                        params,
                    )
            except self._duplicate():  # inserted concurrently: last writer wins, as in Redis
                conn.execute(update, params)

    def delete(self, key: str) -> bool:
        with self.engine.begin() as conn:
            gone = conn.execute(
                self._sql(f"DELETE FROM {self.table} WHERE idem_key=:k"),  # nosec B608
                {"k": digest(key)},
            )
            return bool(gone.rowcount)

    def purge_expired(self, limit: int = 1000) -> int:
        """Delete up to ``limit`` expired rows; returns how many went. Call it on a schedule."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        if self.engine.dialect.name == "mssql":
            sql = f"DELETE TOP ({limit}) FROM {self.table} WHERE expires_at<=:now"  # nosec B608
        else:
            sql = (
                f"DELETE FROM {self.table} WHERE idem_key IN (SELECT idem_key FROM {self.table}"  # nosec B608
                f" WHERE expires_at<=:now LIMIT {limit})"
            )
        with self.engine.begin() as conn:
            return int(conn.execute(self._sql(sql), {"now": self._now()}).rowcount)


def idempotent(
    *,
    key: str | Callable[..., Any] | None = None,
    ttl: float | str = "1d",
    store: Any = None,
    backend: Any = None,
    in_progress: str = "raise",
    lease_ttl: float | str = 60,
    timeout: float | None = None,
    connection: Callable[[], Any] | None = None,
) -> Callable[[F], F]:
    """Run once per key within TTL; duplicates return the stored result.

    `in_progress` is raise, wait, or return_none. ``store`` may also be an
    `python_wrap.async_cache.AsyncCacheStore` (for ``async def`` functions): it is
    awaited directly, and an `AsyncRedisStore` gets a matching `AsyncRedisLock`.
    Failed calls release their lease without caching. Distributed use requires BOTH a
    shared result store and shared lease backend; RedisStore(fail_open=False)
    automatically supplies a matching RedisLock unless a backend is explicitly given.
    Other shared stores need an explicitly supplied shared lease backend.
    This cannot guarantee exactly-once external effects across a process crash between
    writing an external system and recording the result. Use transactional outboxes or
    database idempotency keys for that guarantee. Ownership is checked and extended
    immediately before publishing a result, but a generic result store cannot fence
    writes atomically with lease expiry; client I/O timeouts must be shorter than the
    lease and strict transactional guarantees require an application-level protocol.

    ``connection`` closes that gap for work whose effect is a database write. Give a
    `SqlResultStore` and a callable returning the SQLAlchemy ``Connection`` the function
    writes with, inside the caller's transaction. The key row is inserted on that
    connection (in a savepoint) and marked done there, so it commits or rolls back with
    the function's own writes: a crash before commit leaves no key and no write, and a
    retry runs once. A concurrent duplicate waits on the row lock and then returns the
    committed result; a live claim the caller committed early follows ``in_progress``.
    Sync functions only.
    """
    retention, lease_seconds = seconds(ttl), seconds(lease_ttl)
    if in_progress not in {"raise", "wait", "return_none"}:
        raise ValueError("in_progress must be raise, wait or return_none")
    results: Any = MemoryResultStore() if store is None else store
    if getattr(results, "fail_open", False):
        raise ValueError("idempotency requires a result store with fail_open=False")
    if backend is not None and not isinstance(backend, LocalLease) and store is None:
        raise ValueError("a distributed lease requires an explicitly shared result store")
    native = _core.is_async(results.get)  # an AsyncCacheStore: awaited, never threaded
    if backend is None:
        from python_wrap.async_cache import AsyncRedisStore
        from python_wrap.coordination import AsyncRedisLock, RedisLock
        from python_wrap.stores import RedisStore

        if isinstance(results, (RedisStore, AsyncRedisStore)):
            lock = AsyncRedisLock if isinstance(results, AsyncRedisStore) else RedisLock
            backend = lock(
                results.client,
                prefix="python-wrap:idempotency-locks:" + digest(results.prefix) + ":",
            )

    async def read(k: str) -> Any:
        return await (results.get(k) if native else asyncio.to_thread(results.get, k))

    def write(k: str, value: Any) -> Any:
        if native:
            return asyncio.ensure_future(results.set(k, (value,), retention))
        return asyncio.create_task(asyncio.to_thread(results.set, k, (value,), retention))

    if connection is not None and not isinstance(results, SqlResultStore):
        raise TypeError("connection= needs store=SqlResultStore(...)")

    def decorate(func: F) -> F:
        if native and not _core.is_async(func):
            raise TypeError("an async result store needs an async def function")
        if connection is not None:
            if _core.is_async(func):
                raise TypeError("connection= supports sync functions only")
            return cast(
                F,
                _transactional(
                    func, key, results, connection, retention, lease_seconds, in_progress
                ),
            )

        def scope(k: str) -> Lease:
            return Lease(
                "idem:" + k,
                backend=backend,
                ttl=lease_seconds,
                wait=in_progress == "wait",
                timeout=timeout,
            )

        if _core.is_async(func):

            @functools.wraps(func)
            async def aw(*args: Any, **kw: Any) -> Any:
                k = call_key(func, key, args, kw)
                found = await read(k)
                if found is not None:
                    return found[0]
                lease = scope(k)
                try:
                    await lease.__aenter__()
                except AlreadyRunning:
                    if in_progress == "return_none":
                        return None
                    raise
                async with AsyncExitStack() as stack:
                    stack.push_async_exit(lease)
                    found = await read(k)
                    if found is not None:
                        return found[0]
                    value = await func(*args, **kw)
                    # A stalled/failed heartbeat must not publish a stale owner's result.
                    await lease.check_async()
                    _, error, cancelled = await _drain(write(k, value))
                    if cancelled:
                        raise asyncio.CancelledError
                    if error is not None:
                        raise error
                    return value

            return cast(F, aw)

        @functools.wraps(func)
        def sw(*args: Any, **kw: Any) -> Any:
            k = call_key(func, key, args, kw)
            found = results.get(k)
            if found is not None:
                return found[0]
            lease = scope(k)
            try:
                lease.__enter__()
            except AlreadyRunning:
                if in_progress == "return_none":
                    return None
                raise
            with ExitStack() as stack:
                stack.push(lease)
                found = results.get(k)
                if found is not None:
                    return found[0]
                value = func(*args, **kw)
                lease.check()
                results.set(k, (value,), retention)
                return value

        return cast(F, sw)

    return decorate


def _transactional(
    func: Callable[..., Any],
    key: str | Callable[..., Any] | None,
    store: SqlResultStore,
    connection: Callable[[], Any],
    retention: float,
    lease_seconds: float,
    in_progress: str,
) -> Callable[..., Any]:
    """`idempotent` whose key row shares the caller's transaction (see ``connection=``)."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kw: Any) -> Any:
        k = call_key(func, key, args, kw)
        conn = connection()
        found = store._read(conn, k)
        if found is not None:
            return found[0]
        owner = uuid.uuid4().hex
        if not store._claim(conn, k, owner, lease_seconds):
            found = store._read(conn, k)  # a duplicate committed while we waited on its lock
            if found is not None:
                return found[0]
            if in_progress == "return_none":
                return None
            raise AlreadyRunning("idem:" + k)
        value = func(*args, **kw)
        store._finish(conn, k, value, retention, owner)
        return value

    return wrapper
