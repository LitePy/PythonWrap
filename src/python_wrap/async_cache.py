"""Async cache stores and ``amemoize``: caching without blocking the event loop.

``memoize(store=RedisStore(...))`` calls its store synchronously, so on an asyncio event
loop every cache read and write is a blocking network round trip. This module provides
the native async path: an `AsyncCacheStore` protocol whose methods are coroutines, two
implementations (`AsyncMemoryStore`, `AsyncRedisStore` for ``redis.asyncio``-compatible
clients), `ThreadedStore` to run any synchronous store in worker threads, and the
`amemoize` decorator for ``async def`` functions.

Stored entries are opaque ``(value, expires_at)`` pairs; ``expires_at`` is a Unix
timestamp (or None) read through ``_core.wall_time`` so ``fake_clock`` controls it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import copy
import datetime
import functools
import inspect
import math
import pickle
import random
import re
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Hashable
from typing import Any, Protocol, TypeVar, runtime_checkable

from python_wrap import _core, _fork
from python_wrap._backend import BackendMonitor
from python_wrap._runtime import digest, stable_digest
from python_wrap.caching import (
    _TAG_PREFIX,
    CacheInfo,
    _async_tag_clients,
    _async_tag_store_owners,
    _await_shared,
    _decode,
    _exception_types,
    _make_key_builder,
    _register_tag_client,
    _shared_future,
    _tag_versions,
    _ttl_seconds,
)
from python_wrap.stores import KEY_FORMAT, InvalidSignature, json_codec

__all__ = [
    "AsyncCacheStore",
    "AsyncMemoryStore",
    "AsyncRedisStore",
    "ThreadedStore",
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
        dumps: Callable[[Any], bytes] | None = None,
        loads: Callable[[bytes], Any] | None = None,
        key_format: Callable[[Hashable], str] | None = None,
        fail_open: bool = True,
        retry_backend_after: float = 1.0,
        version: int | None = None,
    ) -> None:
        if not prefix:
            raise ValueError("AsyncRedisStore needs a nonempty per-function prefix")
        if version is not None and (
            isinstance(version, bool) or not isinstance(version, int) or version < 1
        ):
            raise ValueError("version must be a positive integer")
        if dumps is None and loads is None:
            from python_wrap.config import get_config

            codec = get_config().get("cache_codec")
            if codec is None:
                import warnings

                warnings.warn(
                    "implicit pickle cache codec is deprecated; pass a codec",
                    DeprecationWarning,
                    stacklevel=2,
                )
                codec = (pickle.dumps, pickle.loads)
            dumps, loads = codec
        self.client, self.prefix = client, prefix + (f"v{version}:" if version else "")
        self.dumps, self.loads = dumps or pickle.dumps, loads or pickle.loads
        self.key_format, self.fail_open = key_format, fail_open
        self._monitor = BackendMonitor(
            "cache", prefix, "allow" if fail_open else "raise", retry_backend_after
        )

    def _key(self, key: Hashable) -> str:
        if self.key_format:
            return self.prefix + self.key_format(key)
        return self.prefix + KEY_FORMAT + stable_digest(key)

    async def _run(self, action: Callable[[], Awaitable[Any]], default: Any = None) -> Any:
        return await self._monitor.acall("cache", action, lambda: default)

    async def get(self, key: Hashable, /) -> Any | None:
        """Return a decoded entry, or None on a miss."""

        async def read() -> Any:
            value = await self.client.get(self._key(key))
            if value is None:
                return None
            try:
                return self.loads(value)
            except InvalidSignature:
                _core.emit("cache.rejected", self.prefix, reason="signature")
                return None

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


class ThreadedStore:
    """Use a synchronous `CacheStore` from async code without blocking the event loop.

    Every ``get``/``set``/``delete``/``clear`` call runs the wrapped store's method in a
    worker thread (``asyncio.to_thread``, or ``executor`` when given), so a network
    store such as `python_wrap.stores.RedisStore` with a plain ``redis.Redis`` client
    becomes an `AsyncCacheStore` for `amemoize`. Prefer `AsyncRedisStore` when a
    ``redis.asyncio`` client is available: it needs no threads.

    The wrapped store must be thread-safe (the `CacheStore` contract already requires
    it). A worker thread cannot be interrupted, so a cancelled caller stops waiting but
    the store call still finishes in the background.

    Args:
        store: Any synchronous `CacheStore`.
        executor: A `concurrent.futures.Executor` to run the calls in; ``None`` uses the
            event loop's default executor (via ``asyncio.to_thread``, which keeps context
            variables).

    Example:
        ```python
        import asyncio

        from python_wrap.async_cache import ThreadedStore, amemoize


        class DictStore:  # stands in for RedisStore(redis.Redis(...), prefix=...)
            def __init__(self) -> None:
                self.data: dict[object, object] = {}

            def get(self, key):
                return self.data.get(key)

            def set(self, key, entry, ttl):
                self.data[key] = entry

            def delete(self, key):
                return self.data.pop(key, None) is not None

            def clear(self):
                self.data.clear()

            def __len__(self):
                return len(self.data)


        calls = []


        @amemoize(store=ThreadedStore(DictStore()))
        async def lookup(user_id: int) -> str:
            calls.append(user_id)
            return f"user-{user_id}"


        async def main() -> None:
            assert await lookup(1) == await lookup(1) == "user-1"
            assert calls == [1]


        asyncio.run(main())
        ```
    """

    def __init__(self, store: Any, *, executor: concurrent.futures.Executor | None = None):
        for method in ("get", "set", "delete", "clear"):
            if not callable(getattr(store, method, None)):
                raise TypeError(f"store must have a callable {method}() method")
            if inspect.iscoroutinefunction(getattr(store, method)):
                raise TypeError("store is already asynchronous; pass it to amemoize directly")
        self.store, self.executor = store, executor

    async def _call(self, func: Callable[..., Any], *args: Any) -> Any:
        if self.executor is None:
            return await asyncio.to_thread(func, *args)
        return await asyncio.get_running_loop().run_in_executor(self.executor, func, *args)

    async def get(self, key: Hashable, /) -> Any | None:
        """Return the wrapped store's entry, read in a worker thread."""
        return await self._call(self.store.get, key)

    async def set(self, key: Hashable, entry: Any, ttl: float | None, /) -> None:
        """Store an entry from a worker thread."""
        await self._call(self.store.set, key, entry, ttl)

    async def delete(self, key: Hashable, /) -> bool:
        """Delete one entry from a worker thread; report whether it existed."""
        return bool(await self._call(self.store.delete, key))

    async def clear(self) -> None:
        """Clear the wrapped store from a worker thread."""
        await self._call(self.store.clear)

    def size(self) -> int | None:
        """The wrapped store's ``size()`` (None if unknown), else ``len()``, else None."""
        size = getattr(self.store, "size", None)
        if callable(size):
            value = size()
            return None if value is None else int(value)
        try:
            return len(self.store)
        except TypeError:
            return None


def _split(entry: Any) -> tuple[Any, float | None, bool, float | None] | None:
    """Validate a stored entry (a JSON codec yields lists); None means unusable.

    Entries are ``(value, expires)`` or, for stale-while-revalidate and cached
    exceptions, ``(value, expires, is_exception, fresh_until)``.
    """
    if isinstance(entry, (tuple, list)) and len(entry) == 2:
        return entry[0], entry[1], False, entry[1]
    if isinstance(entry, (tuple, list)) and len(entry) in (4, 5):
        return entry[0], entry[1], bool(entry[2]), entry[3]
    return None


def amemoize(
    *,
    ttl: float | None = None,
    key: Callable[..., Hashable] | None = None,
    store: AsyncCacheStore | None = None,
    cache_none: bool = True,
    copy_result: bool = False,
    singleflight: bool = True,
    stale_ttl: float | datetime.timedelta | None = None,
    cache_exceptions: type[BaseException] | tuple[type[BaseException], ...] = (),
    exception_ttl: float | datetime.timedelta | None = None,
    logger: _core.LoggerLike = None,
    lock: Any = None,
    lock_ttl: float = 30,
    lock_wait: float = 5,
    tags: Callable[..., Any] | None = None,
    ttl_jitter: float = 0.0,
) -> Callable[[F], F]:
    """Cache the results of an ``async def`` function in an `AsyncCacheStore`.

    ``stale_ttl``, ``cache_exceptions`` and ``exception_ttl`` behave as in
    `python_wrap.caching.memoize`: a stale entry is served at once while one background
    task refreshes it (a failed refresh keeps the stale value, logs a warning and emits
    ``cache.refresh_failed``), and a listed exception is stored and re-raised on hits.
    Cached exceptions are never served stale; a shared store needs a codec that can
    encode them.

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
        stale_ttl: Seconds after ``ttl`` during which the stale value is still served
            while a background refresh runs. Requires ``ttl``.
        cache_exceptions: Exception class (or tuple) cached and re-raised like results.
            ``asyncio.CancelledError`` never is.
        exception_ttl: Seconds a cached exception stays valid; defaults to ``ttl``.
        logger: Logger (or name) for failed background refreshes.
        lock: A lease backend shared by every process (prefer `AsyncRedisLock`): one
            process computes a missing key, the others wait up to ``lock_wait`` and read
            the store again. Stale refreshes take it without waiting. Needs ``store``.
        lock_ttl: Seconds the stampede lease lives (renewed while computing).
        lock_wait: Seconds a process waits for another's computation.
        tags: ``tags(*args, **kwargs)`` returning tag names; see
            `python_wrap.caching.invalidate_tags` and ``ainvalidate_tags``.
        ttl_jitter: Shorten each ttl by a random share up to this (0 to 1).

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
    if stale_ttl is not None and ttl is None:
        raise ValueError("stale_ttl requires ttl")
    if lock is not None and store is None:
        raise ValueError("lock= coordinates processes through a shared store; pass store=")
    if not 0 <= ttl_jitter < 1:
        raise ValueError("ttl_jitter must be in [0, 1)")
    tag_client: Any = getattr(store, "client", None) if tags is not None else None
    tag_async = tag_client is not None and _core.is_async(tag_client.get)
    if tag_async:
        _register_tag_client(_async_tag_clients, _async_tag_store_owners, tag_client, store)
    elif tag_client is not None:
        from python_wrap.caching import _tag_clients, _tag_store_owners

        _register_tag_client(_tag_clients, _tag_store_owners, tag_client, store)

    async def versions(names: list[str]) -> dict[str, str] | None:
        if not tag_async:
            return _tag_versions(names, tag_client)
        try:
            return {n: _decode(await tag_client.get(_TAG_PREFIX + digest(n))) for n in names}
        except Exception:  # unknown versions read as a miss
            return None

    stale_s = _ttl_seconds(stale_ttl)
    exc_types = _exception_types(cache_exceptions)
    exc_ttl = _ttl_seconds(exception_ttl) if exception_ttl is not None else ttl

    def decorate(func: F) -> F:
        if not inspect.iscoroutinefunction(func):
            raise TypeError("amemoize only supports async def functions; use memoize for sync")
        backend: AsyncCacheStore = store if store is not None else AsyncMemoryStore()
        build_key = _make_key_builder(func, False, key)
        name = _core.qualname(func)
        hits = misses = 0
        stats_lock = threading.Lock()
        inflight: dict[tuple[int, Hashable], asyncio.Future[Any]] = {}
        background: set[asyncio.Future[Any]] = set()  # strong refs to refresh tasks

        def isolate(value: Any) -> Any:
            return copy.deepcopy(value) if copy_result else value

        def lookup(raw: Any) -> tuple[bool, Any, bool, bool]:
            """``(found, value, is_exception, stale)`` for a raw store entry."""
            parts = _split(raw)
            if parts is None:
                return False, None, False, False
            value, expires, is_exc, fresh = parts
            now = _core.wall_time()
            if expires is not None and now >= expires:
                return False, None, False, False
            return True, value, is_exc, fresh is not None and now >= fresh

        def count(hit: bool) -> None:
            nonlocal hits, misses
            with stats_lock:
                if hit:
                    hits += 1
                else:
                    misses += 1
            if _core.listeners:
                _core.emit("cache.hit" if hit else "cache.miss", name)

        async def save(k: Hashable, value: Any, is_exc: bool, tv: Any = None) -> None:
            life = exc_ttl if is_exc else ttl
            if ttl_jitter and life:
                life *= 1 - random.random() * ttl_jitter  # nosec B311 - not security
            keep = life if is_exc or life is None or stale_s is None else life + stale_s
            now = _core.wall_time()
            expires = None if keep is None else now + keep
            fresh = None if life is None else now + life
            plain = not is_exc and fresh == expires and tags is None  # old readers: 2-tuples
            entry: tuple[Any, ...] = (value, expires) if plain else (value, expires, is_exc, fresh)
            if tags is not None:
                entry = (*entry, tv)
            await backend.set(k, entry, keep)

        async def run(k: Hashable, args: Any, kwargs: Any) -> Any:
            tv = None if tags is None else await versions(list(tags(*args, **kwargs)))
            try:
                result = await func(*args, **kwargs)
            except exc_types as exc:
                if not isinstance(exc, asyncio.CancelledError):
                    await save(k, exc, True, tv)
                raise
            if result is not None or cache_none:
                await save(k, isolate(result), False, tv)
            return result

        def lease_for(k: Hashable, wait: bool) -> Any:
            from python_wrap.coordination import Lease

            return Lease(
                f"amemo:{digest(name)}:{stable_digest(k)}",
                backend=lock,
                ttl=lock_ttl,
                wait=wait,
                timeout=lock_wait if wait else None,
            )

        async def compute(k: Hashable, args: Any, kwargs: Any, refreshing: bool = False) -> Any:
            """Compute and store, once across processes when ``lock`` is set."""
            if lock is None:
                return await run(k, args, kwargs)
            from python_wrap.coordination import AlreadyRunning

            lease = lease_for(k, not refreshing)
            try:
                await lease.__aenter__()
            except AlreadyRunning:
                lease = None
            try:
                if lease is None or not refreshing:
                    found, value, is_exc, stale = await read(k)
                    if found and (lease is None or not stale):
                        if is_exc:
                            raise value
                        return value  # another process computed (or is refreshing) it
                return await run(k, args, kwargs)
            finally:
                if lease is not None:
                    with contextlib.suppress(Exception):
                        await lease.__aexit__(None, None, None)

        async def read(k: Hashable) -> tuple[bool, Any, bool, bool]:
            raw = await backend.get(k)
            found, value, is_exc, stale = lookup(raw)
            if found and tags is not None:
                recorded = raw[4] if isinstance(raw, (tuple, list)) and len(raw) == 5 else None
                if recorded and await versions(list(recorded)) != recorded:
                    return False, None, False, False
            return found, value, is_exc, stale

        def start(
            fkey: tuple[int, Hashable],
            k: Hashable,
            args: Any,
            kwargs: Any,
            refreshing: bool = False,
        ) -> Any:
            """Run one computation for ``fkey`` and return the future its callers share."""
            shared = _shared_future()
            inflight[fkey] = shared

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
                task = asyncio.ensure_future(compute(k, args, kwargs, refreshing))
            except BaseException:
                inflight.pop(fkey, None)
                raise
            task.add_done_callback(finish)
            return shared

        def refresh(k: Hashable, args: Any, kwargs: Any) -> None:
            fkey = (id(asyncio.get_running_loop()), k)
            if fkey in inflight:
                return  # a refresh (or a miss) for this key is already running
            shared = start(fkey, k, args, kwargs, refreshing=True)

            def failed(fut: asyncio.Future[Any]) -> None:
                if not fut.cancelled() and (exc := fut.exception()) is not None:
                    _core.get_logger(logger).warning(
                        "amemoize: background refresh of %s failed; keeping the stale value",
                        name,
                        exc_info=exc,
                    )
                    _core.emit("cache.refresh_failed", name, exception=exc)

            shared.add_done_callback(failed)
            background.add(shared)
            shared.add_done_callback(background.discard)
            if _core.listeners:
                _core.emit("cache.stale", name)

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if _core.overrides.disable_caching:
                return await func(*args, **kwargs)
            k = build_key(args, kwargs)
            found, value, is_exc, stale = await read(k)
            if found:
                count(True)
                if stale and not is_exc:
                    refresh(k, args, kwargs)
                if is_exc:
                    raise value
                return isolate(value)
            if not singleflight:
                count(False)
                return await compute(k, args, kwargs)
            fkey = (id(asyncio.get_running_loop()), k)
            shared = inflight.get(fkey)
            if shared is not None:
                count(True)
                return isolate(await _await_shared(shared))
            count(False)
            return isolate(await _await_shared(start(fkey, k, args, kwargs)))

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
