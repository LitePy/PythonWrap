"""Caching decorators: :func:`memoize`, :func:`cache` and :func:`once`."""

from __future__ import annotations

import asyncio
import datetime
import functools
import inspect
import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any, NamedTuple, ParamSpec, Protocol, TypeVar, overload

from python_wrap import _core

__all__ = ["CacheInfo", "cache", "memoize", "once"]

P = ParamSpec("P")
R = TypeVar("R")
R_co = TypeVar("R_co", covariant=True)

_MISSING: Any = object()


class CacheInfo(NamedTuple):
    """Statistics returned by ``wrapped.cache_info()``."""

    hits: int
    misses: int
    maxsize: int | None
    currsize: int


class Memoized(Protocol[P, R_co]):
    """A function wrapped by :func:`memoize` (callable plus cache controls)."""

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...

    def __get__(self, instance: object, owner: type | None = None) -> Memoized[..., R_co]: ...

    def cache_clear(self) -> None:
        """Drop every cached entry and reset the statistics."""

    def cache_info(self) -> CacheInfo:
        """Return hits, misses, maxsize and the current number of live entries."""

    def cache_delete(self, *args: Any, **kwargs: Any) -> bool:
        """Drop the entry for these arguments. Returns True if one existed."""


class Once(Protocol[P, R_co]):
    """A function wrapped by :func:`once`."""

    called: bool

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...

    def __get__(self, instance: object, owner: type | None = None) -> Once[..., R_co]: ...

    def reset(self) -> None:
        """Forget the stored result so the next call runs the function again."""


# --------------------------------------------------------------------------- keys


class _Tag:
    """Marker that keeps ``[1]`` and ``(1,)`` (or ``{1}``) from sharing a key."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"<{self.name}>"


_LIST, _DICT, _SET = _Tag("list"), _Tag("dict"), _Tag("set")


def _freeze(value: Any) -> Hashable:
    """Make ``value`` hashable: builtin containers are converted recursively."""
    try:
        hash(value)
    except TypeError:
        pass
    else:
        return value  # type: ignore[no-any-return]
    if isinstance(value, list):
        return (_LIST, tuple(_freeze(v) for v in value))
    if isinstance(value, tuple):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, dict):
        return (_DICT, frozenset((_freeze(k), _freeze(v)) for k, v in value.items()))
    if isinstance(value, (set, frozenset)):
        return (_SET, frozenset(_freeze(v) for v in value))
    raise TypeError(
        f"memoize: argument of type {type(value).__name__!r} is not hashable; "
        "pass key=<callable> to build the cache key yourself"
    )


def _make_key_builder(
    func: Callable[..., Any], typed: bool, key: Callable[..., Hashable] | None
) -> Callable[[tuple[Any, ...], dict[str, Any]], Hashable]:
    if key is not None:
        user_key = key

        def custom(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Hashable:
            return user_key(*args, **kwargs)

        return custom

    try:
        sig: inspect.Signature | None = inspect.signature(func)
    except (TypeError, ValueError):
        sig = None

    def build(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Hashable:
        if sig is not None:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            values = list(bound.arguments.values())
        else:
            values = [*args, *sorted(kwargs.items())]
        frozen = tuple(_freeze(v) for v in values)
        if typed:
            return (frozen, tuple(type(v) for v in values))
        return frozen

    return build


# ------------------------------------------------------------------------ memoize


def _ttl_seconds(ttl: float | datetime.timedelta | None) -> float | None:
    if ttl is None:
        return None
    seconds = ttl.total_seconds() if isinstance(ttl, datetime.timedelta) else float(ttl)
    if seconds <= 0:
        raise ValueError("ttl must be positive")
    return seconds


@overload
def memoize(func: Callable[P, R], /) -> Memoized[P, R]: ...
@overload
def memoize(
    func: None = None,
    /,
    *,
    maxsize: int | None = None,
    ttl: float | datetime.timedelta | None = None,
    typed: bool = False,
    key: Callable[..., Hashable] | None = None,
) -> Callable[[Callable[P, R]], Memoized[P, R]]: ...
def memoize(
    func: Callable[..., Any] | None = None,
    /,
    *,
    maxsize: int | None = None,
    ttl: float | datetime.timedelta | None = None,
    typed: bool = False,
    key: Callable[..., Hashable] | None = None,
) -> Any:
    """Cache a function's results per argument values.

    Works bare (``@memoize``) or with options. Arguments are bound to the function
    signature, so ``f(1)``, ``f(x=1)`` and ``f(1, y=<default>)`` share one entry.
    Arguments must be hashable; lists, dicts and sets (also nested) are converted
    automatically. Anything else unhashable raises ``TypeError`` -- pass ``key=``.

    Exceptions are never cached. The cache is guarded by a lock. For sync functions two
    threads that miss on the same key at the same time may both compute the value (the
    function itself is never called under the lock); the last result stored wins. For
    ``async def`` functions the awaited result is cached and concurrent awaits of the
    same missing key share a single in-flight task, so it runs once.

    Methods: ``self`` is part of the key, so results are per instance. The cache holds a
    strong reference to every instance it has seen, keeping it alive; use ``maxsize`` or
    ``ttl`` (or ``key=`` ignoring ``self``) on methods of many short-lived objects.

    The wrapper has ``cache_clear()``, ``cache_info()`` (a :class:`CacheInfo`) and
    ``cache_delete(*args, **kwargs)``.

    Args:
        func: The function (when used bare).
        maxsize: Maximum entries; least recently used are evicted. None is unbounded.
        ttl: Seconds (or a ``timedelta``) an entry stays valid. None never expires.
        typed: If True, ``f(1)`` and ``f(1.0)`` are cached separately.
        key: Custom ``key(*args, **kwargs)`` returning a hashable cache key.

    Returns:
        The wrapped function, or a decorator when called with options.

    Raises:
        ValueError: If ``maxsize`` is below 1 or ``ttl`` is not positive.
        TypeError: At call time, for an unhashable argument (see above).

    Example:
        >>> @memoize(maxsize=128, ttl=60)
        ... def square(n):
        ...     return n * n
        >>> square(4), square(n=4)
        (16, 16)
        >>> square.cache_info().hits
        1
    """
    if maxsize is not None and maxsize < 1:
        raise ValueError("maxsize must be a positive integer or None")
    ttl_s = _ttl_seconds(ttl)

    def decorate(f: Callable[..., Any]) -> Any:
        build_key = _make_key_builder(f, typed, key)
        store: OrderedDict[Hashable, tuple[Any, float | None]] = OrderedDict()
        inflight: dict[Hashable, asyncio.Future[Any]] = {}
        lock = threading.Lock()
        stats = {"hits": 0, "misses": 0, "gen": 0}

        def lookup(k: Hashable) -> Any:
            # Caller holds the lock.
            entry = store.get(k)
            if entry is None:
                return _MISSING
            value, expires = entry
            if expires is not None and _core.monotonic() >= expires:
                del store[k]
                return _MISSING
            store.move_to_end(k)
            return value

        def save(k: Hashable, value: Any, gen: int) -> None:
            with lock:
                if gen != stats["gen"]:
                    return  # cache_clear() ran while computing
                expires = None if ttl_s is None else _core.monotonic() + ttl_s
                store[k] = (value, expires)
                store.move_to_end(k)
                if maxsize is not None:
                    while len(store) > maxsize:
                        store.popitem(last=False)

        def cache_clear() -> None:
            with lock:
                store.clear()
                inflight.clear()
                stats.update(hits=0, misses=0, gen=stats["gen"] + 1)

        def cache_info() -> CacheInfo:
            with lock:
                now = _core.monotonic()
                for k in [k for k, (_, exp) in store.items() if exp is not None and now >= exp]:
                    del store[k]
                return CacheInfo(stats["hits"], stats["misses"], maxsize, len(store))

        def cache_delete(*args: Any, **kwargs: Any) -> bool:
            k = build_key(args, kwargs)
            with lock:
                return store.pop(k, _MISSING) is not _MISSING

        if _core.is_async(f):

            @functools.wraps(f)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                k = build_key(args, kwargs)
                with lock:
                    value = lookup(k)
                    if value is not _MISSING:
                        stats["hits"] += 1
                        return value
                    task = inflight.get(k)
                    if task is None:
                        stats["misses"] += 1
                        gen = stats["gen"]

                        async def run() -> Any:
                            try:
                                result = await f(*args, **kwargs)
                                save(k, result, gen)
                                return result
                            finally:
                                with lock:
                                    if inflight.get(k) is asyncio.current_task():
                                        del inflight[k]

                        task = asyncio.ensure_future(run())
                        inflight[k] = task
                    else:
                        stats["hits"] += 1
                # shield: one cancelled caller must not cancel the shared computation
                return await asyncio.shield(task)

            wrapper: Any = awrapper
        else:

            @functools.wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                k = build_key(args, kwargs)
                with lock:
                    value = lookup(k)
                    if value is not _MISSING:
                        stats["hits"] += 1
                        return value
                    stats["misses"] += 1
                    gen = stats["gen"]
                result = f(*args, **kwargs)
                save(k, result, gen)
                return result

        wrapper.cache_clear = cache_clear
        wrapper.cache_info = cache_info
        wrapper.cache_delete = cache_delete
        return wrapper

    return decorate if func is None else decorate(func)


def cache(
    duration: datetime.timedelta | float, *, maxsize: int | None = None
) -> Callable[[Callable[P, R]], Memoized[P, R]]:
    """Cache results for ``duration``; an alias of ``memoize(ttl=duration, maxsize=maxsize)``.

    Kept for v0.1 compatibility. Prefer ``memoize(ttl=...)`` in new code; note this name
    also clashes with :func:`functools.cache`. Every decorated function gets its own
    store, even when one ``cache(...)`` result is applied to several functions.

    Args:
        duration: Seconds (or a ``timedelta``) an entry stays valid.
        maxsize: Maximum entries (LRU). None is unbounded.

    Returns:
        A decorator.

    Example:
        >>> @cache(datetime.timedelta(minutes=5))
        ... def load(): ...
    """

    def decorator(func: Callable[P, R]) -> Memoized[P, R]:
        return memoize(ttl=duration, maxsize=maxsize)(func)

    return decorator


# --------------------------------------------------------------------------- once


@overload
def once(func: Callable[P, R], /) -> Once[P, R]: ...
@overload
def once(func: None = None, /) -> Callable[[Callable[P, R]], Once[P, R]]: ...
def once(func: Callable[..., Any] | None = None, /) -> Any:
    """Run the function a single time; later calls return the first result.

    Arguments of later calls are ignored. Thread-safe (double-checked lock) and
    async-safe: concurrent first awaits share one task. If the first call raises, the
    function is not marked done and the next call tries again. The wrapper has
    ``.reset()`` and a ``.called`` flag.

    Args:
        func: The function (the decorator is used bare).

    Returns:
        The wrapped function.

    Example:
        >>> @once
        ... def setup():
        ...     return object()
        >>> setup() is setup()
        True
    """

    def decorate(f: Callable[..., Any]) -> Any:
        lock = threading.RLock()
        state: dict[str, Any] = {"done": False, "result": None, "task": None}

        if _core.is_async(f):

            @functools.wraps(f)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                if state["done"]:
                    return state["result"]
                with lock:
                    if state["done"]:
                        return state["result"]
                    task = state["task"]
                    if task is None:

                        async def run() -> Any:
                            try:
                                result = await f(*args, **kwargs)
                                with lock:
                                    state.update(done=True, result=result)
                                    wrapper.called = True
                                return result
                            finally:
                                with lock:
                                    state["task"] = None

                        task = state["task"] = asyncio.ensure_future(run())
                return await asyncio.shield(task)

            wrapper: Any = awrapper
        else:

            @functools.wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                if state["done"]:
                    return state["result"]
                with lock:
                    if not state["done"]:
                        state["result"] = f(*args, **kwargs)
                        state["done"] = True
                        wrapper.called = True
                    return state["result"]

        wrapper.called = False

        def reset() -> None:
            with lock:
                state.update(done=False, result=None, task=None)
                wrapper.called = False

        wrapper.reset = reset
        return wrapper

    return decorate if func is None else decorate(func)
