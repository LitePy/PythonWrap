"""Result retention guarded by renewable owner-checked leases."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from contextlib import AsyncExitStack, ExitStack
from typing import Any, TypeVar, cast

from python_wrap import _core, _fork
from python_wrap._runtime import call_key, digest, seconds
from python_wrap.coordination import AlreadyRunning, Lease, LocalLease, _drain

__all__ = ["MemoryResultStore", "idempotent"]
F = TypeVar("F", bound=Callable[..., Any])


class MemoryResultStore:
    """A thread-safe expiring store for local idempotency results."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[Any, float]] = {}
        self._lock = _fork.ForkSafeLock()
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self._data.clear()

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
            self._data[key] = (value, _core.monotonic() + ttl)

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._data.pop(key, None) is not None


def idempotent(
    *,
    key: str | Callable[..., Any] | None = None,
    ttl: float | str = "1d",
    store: Any = None,
    backend: Any = None,
    in_progress: str = "raise",
    lease_ttl: float | str = 60,
    timeout: float | None = None,
) -> Callable[[F], F]:
    """Run once per key within TTL; duplicates return the stored result.

    `in_progress` is raise, wait, or return_none. Failed calls release their lease
    without caching. Distributed use requires BOTH a shared result store and shared
    lease backend; RedisStore(fail_open=False) automatically supplies a matching
    RedisLock unless a backend is explicitly given. Other shared stores need an
    explicitly supplied shared lease backend.
    This cannot guarantee exactly-once external effects across a process crash between
    writing an external system and recording the result. Use transactional outboxes or
    database idempotency keys for that guarantee. Ownership is checked and extended
    immediately before publishing a result, but a generic result store cannot fence
    writes atomically with lease expiry; client I/O timeouts must be shorter than the
    lease and strict transactional guarantees require an application-level protocol.
    """
    retention, lease_seconds = seconds(ttl), seconds(lease_ttl)
    if in_progress not in {"raise", "wait", "return_none"}:
        raise ValueError("in_progress must be raise, wait or return_none")
    results = MemoryResultStore() if store is None else store
    if getattr(results, "fail_open", False):
        raise ValueError("idempotency requires a result store with fail_open=False")
    if backend is not None and not isinstance(backend, LocalLease) and store is None:
        raise ValueError("a distributed lease requires an explicitly shared result store")
    if backend is None:
        from python_wrap.coordination import RedisLock
        from python_wrap.stores import RedisStore

        if isinstance(results, RedisStore):
            backend = RedisLock(
                results.client,
                prefix="python-wrap:idempotency-locks:" + digest(results.prefix) + ":",
            )

    def decorate(func: F) -> F:
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
                found = await asyncio.to_thread(results.get, k)
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
                    found = await asyncio.to_thread(results.get, k)
                    if found is not None:
                        return found[0]
                    value = await func(*args, **kw)
                    # A stalled/failed heartbeat must not publish a stale owner's result.
                    await lease.check_async()
                    write = asyncio.create_task(
                        asyncio.to_thread(results.set, k, (value,), retention)
                    )
                    _, error, cancelled = await _drain(write)
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
