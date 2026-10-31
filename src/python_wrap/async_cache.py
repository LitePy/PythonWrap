"""Async cache stores and ``amemoize``: caching without blocking the event loop.

``memoize(store=RedisStore(...))`` calls its store synchronously, so on an asyncio event
loop every cache read and write is a blocking network round trip. This module provides
the native async path: an `AsyncCacheStore` protocol whose methods are coroutines, two
implementations (`AsyncMemoryStore`, `AsyncRedisStore` for ``redis.asyncio``-compatible
clients) and the `amemoize` decorator for ``async def`` functions.

Stored entries are opaque ``(value, expires_at)`` pairs; ``expires_at`` is a Unix
timestamp (or None) read through ``_core.wall_time`` so ``fake_clock`` controls it.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import inspect
import json
import logging
import math
import pickle
import re
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, Protocol, TypeVar, runtime_checkable

from python_wrap import _core, _fork
from python_wrap._runtime import digest
from python_wrap.caching import CacheInfo, _await_shared, _make_key_builder, _shared_future

__all__ = [
    "AsyncCacheStore",
    "AsyncMemoryStore",
    "AsyncRedisStore",
    "amemoize",
    "json_codec",
]

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


@runtime_checkable
class AsyncCacheStore(Protocol):
    """Where `amemoize` keeps its entries. All four methods are coroutines.

    * ``get(key)`` returns the stored entry or ``None``.
    * ``set(key, entry, ttl)`` stores ``entry``; ``ttl`` is seconds until it is useless
      (``None``: keep until deleted). Entries carry their own deadline, so a store may
      forget entries at any time.
    * ``delete(key)`` returns True if an entry existed.
    * ``clear()`` removes every entry of this store.
    """

    async def get(self, key: Hashable, /) -> Any | None: ...

    async def set(self, key: Hashable, entry: Any, ttl: float | None, /) -> None: ...

    async def delete(self, key: Hashable, /) -> bool: ...

    async def clear(self) -> None: ...


class AsyncMemoryStore:
    """An in-process LRU `AsyncCacheStore` that respects ttl on read.

    Operations never await, so they are atomic with respect to other tasks on the loop;
    a lock also makes the store safe to touch from several threads.

    Args:
        maxsize: Maximum number of entries (least recently used is evicted), or None
            for unbounded.

    Raises:
        ValueError: If ``maxsize`` is not None or a positive integer.

    Example:
        ```python
        import asyncio
        from python_wrap.async_cache import AsyncMemoryStore

        async def main() -> None:
            store = AsyncMemoryStore(maxsize=2)
            await store.set("a", 1, None)
            assert await store.get("a") == 1

        asyncio.run(main())
        ```
    """

    def __init__(self, maxsize: int | None = None) -> None:
        if maxsize is not None and (
            isinstance(maxsize, bool) or not isinstance(maxsize, int) or maxsize < 1
        ):
            raise ValueError("maxsize must be None or an integer >= 1")
        self.maxsize = maxsize
        self._data: OrderedDict[Hashable, tuple[Any, float | None]] = OrderedDict()
        self._lock = _fork.ForkSafeLock()

    async def get(self, key: Hashable, /) -> Any | None:
        """Return the entry, or None if missing or past its ttl."""
        with self._lock:
            found = self._data.get(key)
            if found is None:
                return None
            entry, deadline = found
            if deadline is not None and _core.wall_time() >= deadline:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return entry

    async def set(self, key: Hashable, entry: Any, ttl: float | None, /) -> None:
        """Store an entry; a non-positive ttl removes it."""
        with self._lock:
            if ttl is not None and ttl <= 0:
                self._data.pop(key, None)
                return
            deadline = None if ttl is None else _core.wall_time() + ttl
            self._data[key] = (entry, deadline)
            self._data.move_to_end(key)
            while self.maxsize is not None and len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    async def delete(self, key: Hashable, /) -> bool:
        """Delete one entry; report whether it existed."""
        with self._lock:
            return self._data.pop(key, None) is not None

    async def clear(self) -> None:
        """Remove every entry."""
        with self._lock:
            self._data.clear()

    def size(self) -> int:
        """Return the number of stored entries (including not yet purged expired ones)."""
        with self._lock:
            return len(self._data)

    def __len__(self) -> int:
        return self.size()


def json_codec() -> tuple[Callable[[Any], bytes], Callable[[bytes], Any]]:
    """Return ``(dumps, loads)`` that store entries as JSON instead of pickle.

    Use it with `AsyncRedisStore` when the Redis instance is not fully trusted, since
    unpickling attacker-controlled bytes can execute code. Cached values must be
    JSON-serialisable; tuples come back as lists.

    Example:
        ```python
        from python_wrap.async_cache import json_codec

        dumps, loads = json_codec()
        assert loads(dumps([{"a": 1}, None])) == [{"a": 1}, None]
        ```
    """

    def dumps(value: Any) -> bytes:
        return json.dumps(value, separators=(",", ":")).encode("utf-8")

    def loads(data: bytes) -> Any:
        return json.loads(data)

    return dumps, loads


class AsyncRedisStore:
    """A namespaced `AsyncCacheStore` for ``redis.asyncio``-compatible clients.

    Mirrors `RedisStore`: opaque hashed keys under ``prefix``, millisecond ttl, and a
    ``clear()`` that scans only this namespace and deletes in bounded batches (UNLINK
    where available).

    Warning:
        The default serializer is pickle. Only use it with a Redis instance you fully
        trust; pass ``dumps``/``loads`` from `json_codec` otherwise.

    Args:
        client: A ``redis.asyncio`` client (awaitable get/set/delete, optionally unlink,
            and an async-iterator ``scan_iter``).
        prefix: A nonempty namespace unique to one cached function.
        dumps: Serializer for entries; defaults to pickle.
        loads: Deserializer matching dumps.
        key_format: Optional function turning a cache key into a stable external key.
        fail_open: Treat backend errors as misses/no-ops and log them, rather than raise.

    Raises:
        ValueError: If ``prefix`` is empty.
    """

    def __init__(
        self,
        client: Any,
        prefix: str,
        *,
        dumps: Callable[[Any], bytes] = pickle.dumps,
        loads: Callable[[bytes], Any] = pickle.loads,
        key_format: Callable[[Hashable], str] | None = None,
        fail_open: bool = True,
    ) -> None:
        if not prefix:
            raise ValueError("AsyncRedisStore needs a nonempty per-function prefix")
        self.client, self.prefix = client, prefix
        self.dumps, self.loads = dumps, loads
        self.key_format, self.fail_open = key_format, fail_open

    def _key(self, key: Hashable) -> str:
        return self.prefix + (self.key_format(key) if self.key_format else digest(key))

    async def _run(self, action: Callable[[], Awaitable[Any]], default: Any = None) -> Any:
        try:
            return await action()
        except Exception:
            if not self.fail_open:
                raise
            logging.getLogger("python_wrap").warning(
                "AsyncRedisStore operation failed", exc_info=True
            )
            return default

    async def get(self, key: Hashable, /) -> Any | None:
        """Return a decoded entry, or None on a miss."""

        async def read() -> Any:
            value = await self.client.get(self._key(key))
            return None if value is None else self.loads(value)

        return await self._run(read)

    async def set(self, key: Hashable, entry: Any, ttl: float | None, /) -> None:
        """Store an entry with millisecond TTL precision."""
        if ttl is not None and ttl <= 0:
            await self.delete(key)
            return
        options = {} if ttl is None else {"px": max(1, math.ceil(ttl * 1000))}

        async def write() -> None:
            await self.client.set(self._key(key), self.dumps(entry), **options)

        await self._run(write)

    async def delete(self, key: Hashable, /) -> bool:
        """Delete one entry; report whether it existed."""

        async def remove() -> Any:
            return await self.client.delete(self._key(key))

        return bool(await self._run(remove, False))

    async def clear(self) -> None:
        """Delete this namespace in batches, using UNLINK where supported."""

        async def clear() -> None:
            pattern = re.sub(r"([\\*?\[\]])", r"\\\1", self.prefix) + "*"
            batch: list[Any] = []
            delete = getattr(self.client, "unlink", None) or self.client.delete
            async for key in self.client.scan_iter(match=pattern, count=200):
                batch.append(key)
                if len(batch) == 200:
                    await delete(*batch)
                    batch.clear()
            if batch:
                await delete(*batch)

        await self._run(clear)

    def size(self) -> None:
        """Return None: an exact distributed entry count is intentionally not scanned."""
        return None


def _split(entry: Any) -> tuple[Any, float | None] | None:
    """Validate a stored entry (a JSON codec yields lists); None means unusable."""
    if isinstance(entry, (tuple, list)) and len(entry) == 2:
        return entry[0], entry[1]
    return None


def amemoize(
    *,
    ttl: float | None = None,
    key: Callable[..., Hashable] | None = None,
    store: AsyncCacheStore | None = None,
    cache_none: bool = True,
    copy_result: bool = False,
    singleflight: bool = True,
) -> Callable[[F], F]:
    """Cache the results of an ``async def`` function in an `AsyncCacheStore`.

    Concurrent misses for one key on one event loop share a single computation when
    ``singleflight`` is true. Exceptions are never cached and reach every waiter;
    cancelling one waiter does not cancel the shared computation for the others. While
    ``python_wrap.testing.disable_caching()`` is active the store is neither read nor
    written. Emits ``cache.hit`` and ``cache.miss`` events (sharing an in-flight
    computation counts as a hit).

    The wrapper has ``await cache_clear()``, ``await cache_delete(*args, **kwargs)`` and
    ``cache_info()``.

    Args:
        ttl: Seconds an entry stays valid, or None for no expiry.
        key: Optional function with the wrapped signature returning a hashable key.
        store: Where to keep entries; defaults to a private `AsyncMemoryStore`.
        cache_none: Cache ``None`` results. If False, None is recomputed every time.
        copy_result: Deepcopy values on write and on delivery.
        singleflight: Share one in-flight computation between concurrent misses.

    Raises:
        TypeError: If the decorated function is not ``async def``, or an option has the
            wrong type.
        ValueError: If ``ttl`` is not positive and finite.

    Example:
        ```python
        import asyncio
        from python_wrap.async_cache import amemoize

        calls = 0

        @amemoize(ttl=60)
        async def square(n: int) -> int:
            global calls
            calls += 1
            return n * n

        async def main() -> None:
            assert await square(4) == 16
            assert await square(4) == 16
            assert calls == 1

        asyncio.run(main())
        ```
    """
    if ttl is not None and (
        isinstance(ttl, bool)
        or not isinstance(ttl, (int, float))
        or not math.isfinite(ttl)
        or ttl <= 0
    ):
        raise ValueError("ttl must be None or a positive finite number")
    for opt, val in (
        ("cache_none", cache_none),
        ("copy_result", copy_result),
        ("singleflight", singleflight),
    ):
        if not isinstance(val, bool):
            raise TypeError(f"{opt} must be bool")

    def decorate(func: F) -> F:
        if not inspect.iscoroutinefunction(func):
            raise TypeError("amemoize only supports async def functions; use memoize for sync")
        backend: AsyncCacheStore = store if store is not None else AsyncMemoryStore()
        build_key = _make_key_builder(func, False, key)
        name = _core.qualname(func)
        hits = misses = 0
        stats_lock = threading.Lock()
        inflight: dict[tuple[int, Hashable], asyncio.Future[Any]] = {}

        def isolate(value: Any) -> Any:
            return copy.deepcopy(value) if copy_result else value

        def lookup(raw: Any) -> tuple[bool, Any]:
            parts = _split(raw)
            if parts is None:
                return False, None
            value, deadline = parts
            if deadline is not None and _core.wall_time() >= deadline:
                return False, None
            return True, value

        def count(hit: bool) -> None:
            nonlocal hits, misses
            with stats_lock:
                if hit:
                    hits += 1
                else:
                    misses += 1
            if _core.listeners:
                _core.emit("cache.hit" if hit else "cache.miss", name)

        async def compute(k: Hashable, args: Any, kwargs: Any) -> Any:
            result = await func(*args, **kwargs)
            if result is not None or cache_none:
                deadline = None if ttl is None else _core.wall_time() + ttl
                await backend.set(k, (isolate(result), deadline), ttl)
            return result

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _core.overrides.disable_caching:
                return await func(*args, **kwargs)
            k = build_key(args, kwargs)
            found, value = lookup(await backend.get(k))
            if found:
                count(True)
                return isolate(value)
            if not singleflight:
                count(False)
                return await compute(k, args, kwargs)
            fkey = (id(asyncio.get_running_loop()), k)
            shared = inflight.get(fkey)
            if shared is not None:
                count(True)
                return isolate(await _await_shared(shared))
            shared = _shared_future()
            inflight[fkey] = shared
            count(False)

            def finish(task: asyncio.Future[Any]) -> None:
                if inflight.get(fkey) is shared:
                    del inflight[fkey]
                if shared.done():
                    if not task.cancelled():
                        task.exception()
                elif task.cancelled():
                    shared.cancel()
                elif (exc := task.exception()) is not None:
                    shared.set_exception(exc)
                else:
                    shared.set_result(task.result())

            try:
                task = asyncio.ensure_future(compute(k, args, kwargs))
            except BaseException:
                inflight.pop(fkey, None)
                raise
            task.add_done_callback(finish)
            return isolate(await _await_shared(shared))

        async def cache_clear() -> None:
            """Remove every entry from the store and reset the statistics."""
            nonlocal hits, misses
            await backend.clear()
            with stats_lock:
                hits = misses = 0

        async def cache_delete(*args: Any, **kwargs: Any) -> bool:
            """Remove the entry for these arguments; True if one existed."""
            return await backend.delete(build_key(args, kwargs))

        def cache_info() -> CacheInfo:
            """Return hits, misses, maxsize and current size (None if unknown)."""
            size = getattr(backend, "size", None)
            with stats_lock:
                return CacheInfo(
                    hits,
                    misses,
                    getattr(backend, "maxsize", None),
                    size() if callable(size) else None,
                )

        wrapper.cache_clear = cache_clear  # type: ignore[attr-defined]
        wrapper.cache_delete = cache_delete  # type: ignore[attr-defined]
        wrapper.cache_info = cache_info  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorate
