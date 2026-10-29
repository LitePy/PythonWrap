"""Local and distributed owner-checked leases and single-instance decorators."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import os
import threading
import uuid
from collections.abc import Callable
from typing import Any, TypeVar, cast

from python_wrap import _core, _fork
from python_wrap._runtime import call_key, seconds

__all__ = [
    "AlreadyRunning",
    "Lease",
    "LeaseLost",
    "LocalLease",
    "PgAdvisoryLock",
    "RedisLock",
    "RedisSemaphore",
    "SqlAppLock",
    "single_instance",
]
F = TypeVar("F", bound=Callable[..., Any])


class AlreadyRunning(RuntimeError):
    """The requested lease could not be acquired."""


class LeaseLost(RuntimeError):
    """A lease expired or could not be renewed while work was running."""


class LocalLease:
    """In-process leases; concurrent acquisition and renewal are owner-checked."""

    def __init__(self) -> None:
        self._lock = _fork.ForkSafeLock()
        self._entries: dict[str, dict[str, float]] = {}
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._entries.clear()

    def acquire(self, key: str, token: str, ttl: float, limit: int = 1) -> bool:
        _validate_request(key, token, ttl, limit)
        with self._lock:
            now = _core.monotonic()
            entries = self._entries.setdefault(key, {})
            for old in list(entries):
                if entries[old] <= now:
                    del entries[old]
            if token not in entries and len(entries) >= limit:
                return False
            entries[token] = now + ttl
            return True

    def renew(self, key: str, token: str, ttl: float) -> bool:
        _validate_request(key, token, ttl, 1)
        with self._lock:
            entries = self._entries.get(key, {})
            if entries.get(token, 0) <= _core.monotonic():
                return False
            entries[token] = _core.monotonic() + ttl
            return True

    def release(self, key: str, token: str) -> bool:
        with self._lock:
            entries = self._entries.get(key, {})
            expires = entries.pop(token, None)
            if not entries:
                self._entries.pop(key, None)
            return expires is not None and expires > _core.monotonic()


def _validate_request(key: str, token: str, ttl: float, limit: int) -> None:
    if not isinstance(key, str) or not key or not isinstance(token, str) or not token:
        raise ValueError("lease key and token must be nonempty strings")
    seconds(ttl)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("lease capacity must be a positive integer")


_LEASE = """
local t=redis.call('TIME'); local now=t[1]*1000+math.floor(t[2]/1000)
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now)
local op=ARGV[1]; local token=ARGV[2]; local ttl=tonumber(ARGV[3]); local limit=tonumber(ARGV[4])
if op=='release' then return redis.call('ZREM',KEYS[1],token) end
if op=='renew' then
 if not redis.call('ZSCORE',KEYS[1],token) then return 0 end
elseif not redis.call('ZSCORE',KEYS[1],token) and redis.call('ZCARD',KEYS[1])>=limit then
 return 0 end
redis.call('ZADD',KEYS[1],now+ttl,token)
local last=redis.call('ZRANGE',KEYS[1],-1,-1,'WITHSCORES')
redis.call('PEXPIRE',KEYS[1],math.max(1,tonumber(last[2])-now))
return 1
"""


class RedisLock:
    """Redis-server-clock leases, acquired/renewed/released atomically with Lua.

    Each acquisition has a unique token. Expired owners cannot release a successor's
    lease. A finite lease cannot guarantee exactly-once side effects after expiry.
    """

    def __init__(self, client: Any, prefix: str = "python-wrap:lease:") -> None:
        if not prefix:
            raise ValueError("prefix must not be empty")
        self.client, self.prefix = client, prefix

    def _call(self, op: str, key: str, token: str, ttl: float, limit: int = 1) -> bool:
        _validate_request(key, token, ttl, limit)
        return bool(
            self.client.eval(
                _LEASE, 1, self.prefix + key, op, token, max(1, math.ceil(ttl * 1000)), limit
            )
        )

    def acquire(self, key: str, token: str, ttl: float, limit: int = 1) -> bool:
        return self._call("acquire", key, token, ttl, limit)

    def renew(self, key: str, token: str, ttl: float) -> bool:
        return self._call("renew", key, token, ttl)

    def release(self, key: str, token: str) -> bool:
        return self._call("release", key, token, 1)


class RedisSemaphore(RedisLock):
    """The same atomic lease backend, with capacity supplied by Lease(limit=...)."""


class _SQLLock:
    dialect: str

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self._connections: dict[str, tuple[str, Any, Any]] = {}
        self._lock = _fork.ForkSafeLock()
        self._pid = os.getpid()

    def _process(self) -> None:
        if os.getpid() != self._pid:
            raise RuntimeError("create SQL lock engines and backends after fork")

    def _resource(self, key: str) -> Any:
        if self.dialect == "postgresql":
            return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
        # SQL Server truncates applock resource names to 255 characters. Hashing
        # avoids unrelated long application keys silently sharing one lock.
        return hashlib.sha256(key.encode()).hexdigest()

    def _placeholder(self) -> str:
        dialect = getattr(self.engine, "dialect", None)
        style = getattr(dialect, "paramstyle", "qmark")
        return "%s" if style in {"format", "pyformat"} else "?"

    @staticmethod
    def _discard(conn: Any) -> None:
        # Closing a SQLAlchemy Connection normally returns its session to the pool.
        # On uncertain unlock/acquire outcomes that session must instead be destroyed.
        try:
            invalidate = getattr(conn, "invalidate", None)
            if invalidate is not None:
                invalidate()
        finally:
            conn.close()

    def acquire(self, key: str, token: str, ttl: float, limit: int = 1) -> bool:
        self._process()
        _validate_request(key, token, ttl, limit)
        if limit != 1:
            raise ValueError("SQL session locks support capacity one only")
        # A backend owns each connection until its matching key/token releases it.
        # Sessions must never be shared with unrelated application transactions.
        with self._lock:
            if token in self._connections:
                raise RuntimeError("a SQL lease token is already active")
        conn = self.engine.connect()
        try:
            if self.dialect == "postgresql":
                ok = bool(
                    conn.exec_driver_sql(
                        "SELECT pg_try_advisory_lock(%s)", (self._resource(key),)
                    ).scalar()
                )
            else:
                sql = (
                    f"DECLARE @r int; EXEC @r = sp_getapplock @Resource={self._placeholder()}, "
                    "@LockMode='Exclusive', @LockOwner='Session', @LockTimeout=0; SELECT @r"
                )
                ok = int(conn.exec_driver_sql(sql, (self._resource(key),)).scalar()) >= 0
            if ok:
                with self._lock:
                    if token in self._connections:
                        raise RuntimeError("a SQL lease token was acquired concurrently")
                    self._connections[token] = (key, conn, threading.Lock())
                return True
        except BaseException:
            try:
                self._discard(conn)
            except Exception:
                logging.getLogger("python_wrap").exception("SQL lease acquire cleanup failed")
            raise
        conn.close()
        return False

    def renew(self, key: str, token: str, ttl: float) -> bool:
        self._process()
        _validate_request(key, token, ttl, 1)
        with self._lock:
            slot = self._connections.get(token)
        if slot is None or slot[0] != key:
            return False
        _, conn, operation_lock = slot
        with operation_lock:
            with self._lock:
                if self._connections.get(token) is not slot:
                    return False
            if getattr(conn, "invalidated", False) or getattr(conn, "closed", False):
                return False
            try:
                conn.exec_driver_sql("SELECT 1")
                return True
            except Exception:
                return False

    def release(self, key: str, token: str) -> bool:
        self._process()
        with self._lock:
            slot = self._connections.get(token)
            if slot is None or slot[0] != key:
                return False
            del self._connections[token]
        _, conn, operation_lock = slot
        with operation_lock:
            try:
                if getattr(conn, "invalidated", False) or getattr(conn, "closed", False):
                    self._discard(conn)
                    return False
                if self.dialect == "postgresql":
                    released = bool(
                        conn.exec_driver_sql(
                            "SELECT pg_advisory_unlock(%s)", (self._resource(key),)
                        ).scalar()
                    )
                else:
                    sql = (
                        "DECLARE @r int; EXEC @r = sp_releaseapplock "
                        f"@Resource={self._placeholder()}, @LockOwner='Session'; SELECT @r"
                    )
                    released = int(conn.exec_driver_sql(sql, (self._resource(key),)).scalar()) >= 0
            except BaseException:
                try:
                    self._discard(conn)
                except Exception:
                    logging.getLogger("python_wrap").exception("SQL lease release cleanup failed")
                raise
            if released:
                conn.close()
            else:
                self._discard(conn)
            return released


class PgAdvisoryLock(_SQLLock):
    """SQLAlchemy PostgreSQL session advisory locks; TTL is session lifetime.

    Use a dedicated, non-multiplexed connection pool and create engines after fork.
    """

    dialect = "postgresql"


class SqlAppLock(_SQLLock):
    """SQLAlchemy SQL Server session locks (pyodbc/pymssql); TTL is session lifetime.

    Resource names are SHA-256 hashes to avoid the server's 255-character truncation.
    """

    dialect = "mssql"


_LOCAL = LocalLease()


async def _drain(task: asyncio.Task[Any]) -> tuple[Any, BaseException | None, bool]:
    """Settle cleanup even under repeated outside cancellation; remember cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    try:
        return task.result(), None, cancelled
    except BaseException as error:
        return None, error, cancelled


class Lease:
    """Single-use sync/async lease context with renewal and bounded acquisition wait.

    Backend operations need client-level I/O timeouts. Cancellation waits for an
    outstanding acquisition/renewal to settle before owner-checked release. The
    caller's exception/cancellation wins over cleanup errors. A lease cannot cross
    a fork and is not a substitute for fencing tokens for strict write exclusion.
    """

    def __init__(
        self,
        key: str,
        *,
        backend: Any = None,
        ttl: float | str = 60,
        limit: int = 1,
        wait: bool = False,
        timeout: float | None = None,
        renew: bool = True,
    ) -> None:
        self.key, self.backend = key, backend if backend is not None else _LOCAL
        self.ttl = seconds(ttl)
        self.token = uuid.uuid4().hex
        _validate_request(key, self.token, self.ttl, limit)
        if timeout is not None:
            timeout = seconds(timeout, positive=False)
        self.limit, self.wait, self.timeout, self.auto_renew = limit, wait, timeout, renew
        self.lost = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._task: asyncio.Task[None] | None = None
        self._lifecycle = threading.Lock()
        self._entering = self._active = self._finished = False
        self._pid = os.getpid()

    def _prepare(self) -> None:
        if os.getpid() != self._pid:
            raise RuntimeError("lease contexts cannot cross a fork")
        with self._lifecycle:
            if self._entering or self._active or self._finished:
                raise RuntimeError(
                    "Lease contexts are single-use and cannot be entered concurrently"
                )
            self._entering = True

    def _try(self) -> bool:
        try:
            return bool(self.backend.acquire(self.key, self.token, self.ttl, self.limit))
        except BaseException:
            # An acquire may succeed server-side before its response is lost.
            try:
                self.backend.release(self.key, self.token)
            except Exception:
                logging.getLogger("python_wrap").exception("lease acquire cleanup failed")
            raise

    def _renew(self) -> None:
        try:
            self.lost = self.lost or not self.backend.renew(self.key, self.token, self.ttl)
        except Exception:
            self.lost = True

    def check(self) -> None:
        """Check and extend this active lease, raising LeaseLost if ownership is gone."""
        if os.getpid() != self._pid or not self._active:
            raise RuntimeError("lease is not active in this process")
        self._renew()
        if self.lost:
            raise LeaseLost(self.key)

    async def check_async(self) -> None:
        """Async counterpart of check(), offloading synchronous backend I/O."""
        task = asyncio.create_task(asyncio.to_thread(self.check))
        _, error, cancelled = await _drain(task)
        if cancelled:
            raise asyncio.CancelledError
        if error is not None:
            raise error

    def __enter__(self) -> Lease:
        import time

        self._prepare()
        try:
            start = time.monotonic()
            while not self._try():
                left = None if self.timeout is None else self.timeout - (time.monotonic() - start)
                if not self.wait or (left is not None and left <= 0):
                    raise AlreadyRunning(self.key)
                time.sleep(min(0.05, self.ttl / 3, left if left is not None else self.ttl))
            self._active = True
            if self.auto_renew:

                def beat() -> None:
                    while not self._stop.wait(self.ttl / 3):
                        self._renew()
                        if self.lost:
                            return

                self._thread = threading.Thread(target=beat, daemon=True, name="python-wrap-lease")
                try:
                    self._thread.start()
                except BaseException:
                    try:
                        self.backend.release(self.key, self.token)
                    except Exception:
                        logging.getLogger("python_wrap").exception("lease start cleanup failed")
                    self._active, self._finished = False, True
                    raise
            return self
        finally:
            self._entering = False

    def __exit__(self, typ: Any, exc: Any, tb: Any) -> None:
        if os.getpid() != self._pid:
            if exc is None:
                raise RuntimeError("lease contexts cannot cross a fork")
            return
        if not self._active:
            raise RuntimeError("lease exited without an acquisition")
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        try:
            released = self.backend.release(self.key, self.token)
        except Exception:
            if exc is None:
                raise
            logging.getLogger("python_wrap").exception("lease release failed during error cleanup")
            return
        finally:
            self._active, self._finished = False, True
        if exc is None and (self.lost or not released):
            raise LeaseLost(self.key)

    async def _atry(self) -> bool:
        task = asyncio.create_task(asyncio.to_thread(self._try))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            acquired, error, _ = await _drain(task)
            if error is None and acquired:
                cleanup = asyncio.create_task(
                    asyncio.to_thread(self.backend.release, self.key, self.token)
                )
                _, cleanup_error, _ = await _drain(cleanup)
                if cleanup_error is not None:
                    logging.getLogger("python_wrap").warning("cancelled lease cleanup failed")
            raise

    async def __aenter__(self) -> Lease:
        self._prepare()
        try:
            loop = asyncio.get_running_loop()
            start = loop.time()
            while not await self._atry():
                left = None if self.timeout is None else self.timeout - (loop.time() - start)
                if not self.wait or (left is not None and left <= 0):
                    raise AlreadyRunning(self.key)
                await asyncio.sleep(min(0.05, self.ttl / 3, left if left is not None else self.ttl))
            self._active = True
            if self.auto_renew:

                async def beat() -> None:
                    while True:
                        await asyncio.sleep(self.ttl / 3)
                        renewal = asyncio.create_task(asyncio.to_thread(self._renew))
                        try:
                            await asyncio.shield(renewal)
                        except asyncio.CancelledError:
                            await _drain(renewal)
                            raise
                        if self.lost:
                            return

                self._task = asyncio.create_task(beat())
            return self
        finally:
            self._entering = False

    async def __aexit__(self, typ: Any, exc: Any, tb: Any) -> None:
        if os.getpid() != self._pid:
            if exc is None:
                raise RuntimeError("lease contexts cannot cross a fork")
            return
        if not self._active:
            raise RuntimeError("lease exited without an acquisition")

        async def finish() -> bool:
            if self._task is not None:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            return bool(await asyncio.to_thread(self.backend.release, self.key, self.token))

        cleanup = asyncio.create_task(finish())
        try:
            released, error, cancelled = await _drain(cleanup)
        finally:
            self._active, self._finished = False, True
        if cancelled:
            raise asyncio.CancelledError
        if error is not None:
            if exc is None:
                raise error
            logging.getLogger("python_wrap").warning("lease release failed during error cleanup")
            return
        if exc is None and (self.lost or not released):
            raise LeaseLost(self.key)


def single_instance(
    *,
    key: str | Callable[..., Any] | None = None,
    backend: Any = None,
    ttl: float | str = 60,
    wait: bool = False,
    timeout: float | None = None,
    renew: bool = True,
) -> Callable[[F], F]:
    """Run one call per key under an owner-checked local or distributed lease."""
    ttl = seconds(ttl)

    def decorate(func: F) -> F:
        import functools

        def scope(args: tuple[Any, ...], kw: dict[str, Any]) -> Lease:
            return Lease(
                call_key(func, key, args, kw),
                backend=backend,
                ttl=ttl,
                wait=wait,
                timeout=timeout,
                renew=renew,
            )

        if _core.is_async(func):

            @functools.wraps(func)
            async def aw(*args: Any, **kw: Any) -> Any:
                async with scope(args, kw):
                    return await func(*args, **kw)

            return cast(F, aw)

        @functools.wraps(func)
        def sw(*args: Any, **kw: Any) -> Any:
            with scope(args, kw):
                return func(*args, **kw)

        return cast(F, sw)

    return decorate
