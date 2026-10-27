"""Caching decorators: `memoize`, `cache`, `once` and `cached_property`."""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime
import functools
import inspect
import threading
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Hashable
from typing import Any, Concatenate, Generic, NamedTuple, ParamSpec, Protocol, TypeVar, overload

from python_wrap import _core

__all__ = [
    "CacheInfo",
    "CachedProperty",
    "Memoized",
    "Once",
    "cache",
    "cached_property",
    "invalidate",
    "memoize",
    "once",
]

P = ParamSpec("P")
R = TypeVar("R")
R_co = TypeVar("R_co", covariant=True)
T = TypeVar("T")
P2 = ParamSpec("P2")

_MISSING: Any = object()


class CacheInfo(NamedTuple):
    """Statistics returned by ``wrapped.cache_info()``."""

    hits: int
    misses: int
    maxsize: int | None
    currsize: int


class Memoized(Protocol[P, R_co]):
    """A function wrapped by `memoize` (callable plus cache controls).

    Use it to annotate decorated functions, e.g. ``Memoized[[int], str]``. The
    ``__get__`` overloads let type checkers bind ``self`` on instance access
    (``obj.method(x)``) and ``cls`` under a ``@classmethod`` stacked above
    (``Cls.method(x)``).

    Typing limitations (runtime behaviour is unaffected):

    * Read from the class (``Cls.method``), the wrapper accepts the call both with and
      without its first argument, because mypy cannot tell a classmethod from an
      instance or static method there. So ``Cls.method(x)`` on an instance method is
      not flagged by mypy (pyright flags it), and ``Cls.cm(Cls, x)`` is accepted by both.
    * Under a ``@staticmethod`` stacked above, ``Cls.method(x)`` type-checks but
      ``obj.method(x)`` is reported as an error by both mypy and pyright.
    """

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...

    @overload
    def __get__(
        self: Memoized[Concatenate[type[T], P2], R_co], instance: None, owner: type[T]
    ) -> _MemoizedOnClass[P, P2, R_co]: ...
    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Memoized[P, R_co]: ...
    @overload
    def __get__(
        self: Memoized[Concatenate[Any, P2], R_co], instance: object, owner: type | None = None
    ) -> Memoized[P2, R_co]: ...

    def cache_clear(self) -> None:
        """Drop every cached entry and reset the statistics."""

    def cache_info(self) -> CacheInfo:
        """Return hits, misses, maxsize and the current number of live entries."""

    def cache_delete(self, *args: Any, **kwargs: Any) -> bool:
        """Drop the entry for these arguments. Returns True if one existed."""

    def cache_contains(self, *args: P.args, **kwargs: P.kwargs) -> bool:
        """True if a live entry exists for these arguments (nothing is computed or counted)."""

    def cache_get(self, *args: P.args, **kwargs: P.kwargs) -> Any:
        """Return the cached result for these arguments without computing it.

        Raises ``KeyError`` when there is no live entry. A cached exception is raised.
        For ``async def`` functions this is the awaited result, not a coroutine.
        """

    def cache_parameters(self) -> dict[str, Any]:
        """The options: ``maxsize``, ``ttl``, ``typed``, ``cache_exceptions``, ``exception_ttl``."""


class _MemoizedOnClass(Memoized[P, R_co], Protocol[P, P2, R_co]):
    """`Memoized` read from a class: accepts the call with and without the first argument.

    Returned for class access (``Cls.attr``) when the first parameter could be ``cls``.
    pyright only picks it under ``@classmethod``; mypy cannot tell a classmethod from an
    instance or static method there, so both call forms are accepted.
    """

    @overload
    def __call__(self, *args: P2.args, **kwargs: P2.kwargs) -> R_co: ...
    @overload
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...


class Once(Protocol[P, R_co]):
    """A function wrapped by `once` (callable plus ``called`` and ``reset()``).

    Binds ``self`` and ``cls`` like `Memoized`, with the same typing limitations.
    """

    called: bool

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...

    @overload
    def __get__(
        self: Once[Concatenate[type[T], P2], R_co], instance: None, owner: type[T]
    ) -> _OnceOnClass[P, P2, R_co]: ...
    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Once[P, R_co]: ...
    @overload
    def __get__(
        self: Once[Concatenate[Any, P2], R_co], instance: object, owner: type | None = None
    ) -> Once[P2, R_co]: ...

    def reset(self) -> None:
        """Forget the stored result so the next call runs the function again."""


class _OnceOnClass(Once[P, R_co], Protocol[P, P2, R_co]):
    """`Once` read from a class: accepts the call with and without the first argument.

    Returned for class access (``Cls.attr``) when the first parameter could be ``cls``.
    pyright only picks it under ``@classmethod``; mypy cannot tell a classmethod from an
    instance or static method there, so both call forms are accepted.
    """

    @overload
    def __call__(self, *args: P2.args, **kwargs: P2.kwargs) -> R_co: ...
    @overload
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...


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
    raise _Unhashable(type(value).__name__)


class _Unhashable(TypeError):
    """Raised by `_freeze`; carries the offending type name."""

    def __init__(self, type_name: str) -> None:
        super().__init__(type_name)
        self.type_name = type_name


def _freeze_arg(name: str, value: Any) -> Hashable:
    try:
        return _freeze(value)
    except _Unhashable as exc:
        raise TypeError(
            f"memoize: argument {name!r} (of type {exc.type_name!r}) is not hashable; "
            "pass key=<callable> to build the cache key yourself (for methods, a key "
            "that ignores or replaces `self`), or use functools.cached_property"
        ) from None


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
            named = list(bound.arguments.items())
        else:
            named = [(f"#{i}", v) for i, v in enumerate(args)]
            named += [(n, v) for n, v in sorted(kwargs.items())]
        values = [v for _, v in named]
        frozen = tuple(_freeze_arg(n, v) for n, v in named)
        if typed:
            return (frozen, tuple(type(v) for v in values))
        return frozen

    return build


# ------------------------------------------------------------------------ memoize


def _retrieve(task: asyncio.Future[Any]) -> None:
    """Mark a shared task's exception as retrieved (all callers may have been cancelled)."""
    if not task.cancelled():
        task.exception()


async def _await_shared(task: asyncio.Future[Any]) -> Any:
    """Await a shared task without letting one caller's cancellation cancel it.

    Unlike ``asyncio.shield`` this never logs "exception in shielded future" when every
    caller was cancelled before a failing task finished.
    """
    outer: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    def forward(t: asyncio.Future[Any]) -> None:
        if outer.done():
            if not t.cancelled():
                t.exception()
        elif t.cancelled():
            outer.cancel()
        elif (exc := t.exception()) is not None:
            outer.set_exception(exc)
        else:
            outer.set_result(t.result())

    task.add_done_callback(forward)
    return await outer


def _shared_future() -> asyncio.Future[Any]:
    """A future of the running loop that in-flight callers share (created under a lock)."""
    fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    fut.add_done_callback(_retrieve)
    return fut


def _copy_outcome(source: asyncio.Future[Any], dest: asyncio.Future[Any]) -> None:
    if dest.done():
        _retrieve(source)
    elif source.cancelled():
        dest.cancel()
    elif (exc := source.exception()) is not None:
        dest.set_exception(exc)
    else:
        dest.set_result(source.result())


def _start(
    coro: Coroutine[Any, Any, Any],
    shared: asyncio.Future[Any],
    on_done: Callable[[asyncio.Future[Any] | None], None],
) -> None:
    """Run ``coro`` as a task and copy its outcome into ``shared``.

    Call it WITHOUT holding a lock: under an eager task factory (Python 3.12+) the
    coroutine may run to completion inside ``ensure_future``. The caller registers
    ``shared`` in its in-flight bookkeeping (under its lock) before calling this, so the
    bookkeeping is correct whether the task starts lazily or eagerly. ``on_done`` gets
    the finished task (or None if no task could be created) and must drop that entry.
    """
    try:
        task = asyncio.ensure_future(coro)
    except BaseException as exc:
        coro.close()
        on_done(None)
        if not shared.done():
            shared.set_exception(exc)
        raise

    def finish(t: asyncio.Future[Any]) -> None:
        try:
            on_done(t)
        finally:
            _copy_outcome(t, shared)

    task.add_done_callback(finish)


def _ttl_seconds(ttl: float | datetime.timedelta | None) -> float | None:
    if ttl is None:
        return None
    seconds = ttl.total_seconds() if isinstance(ttl, datetime.timedelta) else float(ttl)
    if seconds <= 0:
        raise ValueError("ttl must be positive")
    return seconds


def _exception_types(
    value: type[BaseException] | tuple[type[BaseException], ...],
) -> tuple[type[BaseException], ...]:
    types = value if isinstance(value, tuple) else (value,)
    for t in types:
        if not (isinstance(t, type) and issubclass(t, BaseException)):
            raise TypeError("cache_exceptions must be exception classes")
    return types


def _fast_arity(func: Callable[..., Any], typed: bool, key: Callable[..., Hashable] | None) -> int:
    """Number of parameters if ``func`` takes only plain positional ones, else -1.

    For such functions a call that passes every argument positionally can use the
    ``args`` tuple itself as the key (when hashable), skipping signature binding.
    """
    if typed or key is not None:
        return -1
    try:
        params = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return -1
    plain = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    if any(p.kind not in plain for p in params):
        return -1
    return len(params)


# lookup() returns a stored entry ``(value, expires, is_exception)``, ``None`` (absent) or
# this falsy marker (the entry had expired and was just removed).
_EXPIRED: Any = ()


@overload
def memoize(func: Callable[P, R], /) -> Memoized[P, R]: ...
@overload
def memoize(
    func: None = None,
    /,
    *,
    maxsize: int | None = None,
    ttl: float | datetime.timedelta | Callable[[Any], float | None] | None = None,
    typed: bool = False,
    key: Callable[..., Hashable] | None = None,
    cache_exceptions: type[BaseException] | tuple[type[BaseException], ...] = (),
    exception_ttl: float | datetime.timedelta | None = None,
) -> Callable[[Callable[P, R]], Memoized[P, R]]: ...
def memoize(
    func: Callable[..., Any] | None = None,
    /,
    *,
    maxsize: int | None = None,
    ttl: float | datetime.timedelta | Callable[[Any], float | None] | None = None,
    typed: bool = False,
    key: Callable[..., Hashable] | None = None,
    cache_exceptions: type[BaseException] | tuple[type[BaseException], ...] = (),
    exception_ttl: float | datetime.timedelta | None = None,
) -> Any:
    """Cache a function's results per argument values.

    Works bare (``@memoize``) or with options. Arguments are bound to the function
    signature, so ``f(1)``, ``f(x=1)`` and ``f(1, y=<default>)`` share one entry.
    Arguments must be hashable; lists, dicts and sets (also nested) are converted
    automatically. Anything else unhashable raises ``TypeError`` -- pass ``key=``.

    Exceptions are not cached unless listed in ``cache_exceptions``; a matching exception
    is stored and re-raised on every hit as the very same instance (so its traceback
    keeps growing; raise a fresh exception from a ``except`` clause if that matters).
    The cache is guarded by a lock. For sync functions two threads that miss on the same
    key at the same time may both compute the value (the function itself is never called
    under the lock); the last result stored wins. For ``async def`` functions the awaited
    result is cached and concurrent awaits of the same missing key share a single
    in-flight task per event loop, so it runs once.

    A method of a class whose instances are unhashable (for example an ``eq=True``
    dataclass) fails with a ``TypeError`` naming ``self``: ``id(self)`` is deliberately not
    used as a fallback because ids are reused after garbage collection. Pass ``key=`` or
    use `cached_property` instead.

    Methods: ``self`` is part of the key, so results are per instance. The cache holds a
    strong reference to every instance it has seen, keeping it alive; use ``maxsize`` or
    ``ttl`` (or ``key=`` ignoring ``self``) on methods of many short-lived objects.

    The wrapper has ``cache_clear()``, ``cache_info()`` (a `CacheInfo`),
    ``cache_delete(*args, **kwargs)``, ``cache_contains(*args, **kwargs)``,
    ``cache_get(*args, **kwargs)`` and ``cache_parameters()``. ``cache_contains`` and
    ``cache_get`` look an entry up without computing, counting a hit or miss, or
    refreshing its LRU position.

    While ``python_wrap.testing.disable_caching()`` is active the cache is neither read
    nor written. Listeners registered with `python_wrap.events` receive ``cache.hit``,
    ``cache.miss`` and ``cache.evicted`` (field ``reason``: ``"lru"`` or ``"ttl"``).

    Args:
        func: The function (when used bare).
        maxsize: Maximum entries; least recently used are evicted. None is unbounded.
        ttl: Seconds (or a ``timedelta``) an entry stays valid. None never expires. May
            also be a callable ``ttl(result) -> float | None`` evaluated when a result
            is stored (None: never expires; zero or negative: not stored).
        typed: If True, ``f(1)`` and ``f(1.0)`` are cached separately.
        key: Custom ``key(*args, **kwargs)`` returning a hashable cache key.
        cache_exceptions: Exception class (or tuple of classes) whose instances are
            cached and re-raised like results. ``asyncio.CancelledError`` never is.
        exception_ttl: Seconds a cached exception stays valid. Defaults to ``ttl``; it
            must be given when ``ttl`` is a callable and ``cache_exceptions`` is set.

    Returns:
        The wrapped function, or a decorator when called with options.

    Raises:
        ValueError: If ``maxsize`` is below 1 or a ttl is not positive.
        TypeError: For a non-exception in ``cache_exceptions``; at call time, for an
            unhashable argument (see above).

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
    ttl_fn: Callable[[Any], float | None] | None = None
    ttl_s: float | None = None
    if callable(ttl):
        ttl_fn = ttl
    else:
        ttl_s = _ttl_seconds(ttl)
    exc_types = _exception_types(cache_exceptions)
    exc_ttl_s = _ttl_seconds(exception_ttl) if exception_ttl is not None else ttl_s
    if exc_types and ttl_fn is not None and exception_ttl is None:
        raise ValueError("exception_ttl is required when ttl is a callable")
    lru = maxsize is not None
    ov = _core.overrides

    def decorate(f: Callable[..., Any]) -> Any:
        build_key = _make_key_builder(f, typed, key)
        fast_n = _fast_arity(f, typed, key)
        name = _core.qualname(f)
        store: OrderedDict[Hashable, tuple[Any, float | None, bool]] = OrderedDict()
        # In-flight tasks are per event loop: a task cannot be awaited from another loop.
        inflight: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[Hashable, asyncio.Future[Any]]
        ] = weakref.WeakKeyDictionary()
        store_get = store.get
        lock = threading.Lock()
        hits = misses = gen = inserts = live = 0

        def key_of(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Hashable:
            if len(args) == fast_n and not kwargs:
                try:
                    hash(args)
                except TypeError:
                    pass
                else:
                    return args
            return build_key(args, kwargs)

        def sweep() -> int:
            # Caller holds the lock. Drop every expired entry; returns how many.
            now = _core.monotonic()
            dead = [k for k, (_, exp, _e) in store.items() if exp is not None and now >= exp]
            for k in dead:
                del store[k]
            return len(dead)

        def lookup(k: Hashable) -> Any:
            # Caller holds the lock.
            entry = store.get(k)
            if entry is None:
                return None
            expires = entry[1]
            if expires is not None and _core.monotonic() >= expires:
                del store[k]
                return _EXPIRED
            if lru:
                store.move_to_end(k)
            return entry

        def peek(k: Hashable) -> tuple[Any, float | None, bool] | None:
            with lock:
                entry = store.get(k)
            if entry is None or (entry[1] is not None and _core.monotonic() >= entry[1]):
                return None
            return entry

        def report_expired() -> None:
            _core.emit("cache.evicted", name, reason="ttl")

        def save(k: Hashable, value: Any, g: int, is_exc: bool = False) -> None:
            nonlocal inserts, live
            if is_exc:
                seconds = exc_ttl_s
            elif ttl_fn is not None:
                seconds = ttl_fn(value)
                if seconds is not None and seconds <= 0:
                    return  # not worth storing
            else:
                seconds = ttl_s
            ttl_evicted = lru_evicted = 0
            with lock:
                if g != gen:
                    return  # cache_clear() ran while computing
                expires = None if seconds is None else _core.monotonic() + seconds
                store[k] = (value, expires, is_exc)
                if lru:
                    store.move_to_end(k)
                if ttl_s is not None or ttl_fn is not None or exc_ttl_s is not None:
                    # Amortised O(1): a full sweep only after at least
                    # max(64, entries left by the previous sweep) inserts.
                    inserts += 1
                    if inserts >= max(64, live):
                        ttl_evicted = sweep()
                        inserts, live = 0, len(store)
                if maxsize is not None:
                    while len(store) > maxsize:
                        store.popitem(last=False)
                        lru_evicted += 1
            if _core.listeners:
                for _ in range(ttl_evicted):
                    report_expired()
                for _ in range(lru_evicted):
                    _core.emit("cache.evicted", name, reason="lru")

        def cache_clear() -> None:
            nonlocal hits, misses, inserts, live, gen
            with lock:
                store.clear()
                inflight.clear()
                hits = misses = inserts = live = 0
                gen += 1

        def cache_info() -> CacheInfo:
            with lock:
                expired = sweep()
                info = CacheInfo(hits, misses, maxsize, len(store))
            if _core.listeners:
                for _ in range(expired):
                    report_expired()
            return info

        def cache_delete(*args: Any, **kwargs: Any) -> bool:
            k = key_of(args, kwargs)
            with lock:
                return store.pop(k, _MISSING) is not _MISSING

        def cache_contains(*args: Any, **kwargs: Any) -> bool:
            return peek(key_of(args, kwargs)) is not None

        def cache_get(*args: Any, **kwargs: Any) -> Any:
            entry = peek(key_of(args, kwargs))
            if entry is None:
                raise KeyError("no cached entry for these arguments")
            if entry[2]:
                raise entry[0]
            return entry[0]

        def cache_parameters() -> dict[str, Any]:
            return {
                "maxsize": maxsize,
                "ttl": ttl_fn if ttl_fn is not None else ttl_s,
                "typed": typed,
                "cache_exceptions": exc_types,
                "exception_ttl": exc_ttl_s,
            }

        if _core.is_async(f):

            @functools.wraps(f)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                nonlocal hits, misses
                if ov.disable_caching:
                    return await f(*args, **kwargs)
                k = args if len(args) == fast_n and not kwargs else build_key(args, kwargs)
                try:
                    with lock:
                        entry = lookup(k)
                except TypeError:
                    if k is not args:
                        raise
                    k = build_key(args, kwargs)
                    with lock:
                        entry = lookup(k)
                if entry:
                    with lock:
                        hits += 1
                    if _core.listeners:
                        _core.emit("cache.hit", name)
                    if entry[2]:
                        raise entry[0]
                    return entry[0]
                if _core.listeners and entry is not None:
                    report_expired()
                with lock:
                    loop = asyncio.get_running_loop()
                    loop_inflight = inflight.setdefault(loop, {})
                    shared_fut = loop_inflight.get(k)
                    if shared_fut is None:
                        misses += 1
                        g = gen
                        shared = False
                        shared_fut = loop_inflight[k] = _shared_future()
                    else:
                        hits += 1
                        shared = True
                if not shared:

                    async def run() -> Any:
                        try:
                            result = await f(*args, **kwargs)
                        except exc_types as exc:
                            if not isinstance(exc, asyncio.CancelledError):
                                save(k, exc, g, True)
                            raise
                        save(k, result, g)
                        return result

                    def done(_t: asyncio.Future[Any] | None, k: Hashable = k) -> None:
                        with lock:
                            if loop_inflight.get(k) is shared_fut:
                                del loop_inflight[k]

                    _start(run(), shared_fut, done)
                if _core.listeners:
                    _core.emit("cache.hit" if shared else "cache.miss", name)
                # one cancelled caller must not cancel the shared computation
                return await _await_shared(shared_fut)

            wrapper: Any = awrapper
        else:

            @functools.wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                nonlocal hits, misses
                if ov.disable_caching:
                    return f(*args, **kwargs)
                k = args if len(args) == fast_n and not kwargs else build_key(args, kwargs)
                try:
                    with lock:
                        entry = store_get(k)
                        if entry is not None and entry[1] is None:
                            # hot path: a live entry that never expires
                            if lru:
                                store.move_to_end(k)
                            hits += 1
                        else:
                            entry = lookup(k)
                            if entry:
                                hits += 1
                            else:
                                misses += 1
                                g = gen
                except TypeError:
                    if k is not args:
                        raise
                    k = build_key(args, kwargs)
                    with lock:
                        entry = lookup(k)
                        if entry:
                            hits += 1
                        else:
                            misses += 1
                            g = gen
                if entry:
                    if _core.listeners:
                        _core.emit("cache.hit", name)
                    if entry[2]:
                        raise entry[0]
                    return entry[0]
                if _core.listeners:
                    if entry is not None:
                        report_expired()
                    _core.emit("cache.miss", name)
                try:
                    result = f(*args, **kwargs)
                except exc_types as exc:
                    save(k, exc, g, True)
                    raise
                save(k, result, g)
                return result

        wrapper.cache_clear = cache_clear
        wrapper.cache_info = cache_info
        wrapper.cache_delete = cache_delete
        wrapper.cache_contains = cache_contains
        wrapper.cache_get = cache_get
        wrapper.cache_parameters = cache_parameters
        return wrapper

    return decorate if func is None else decorate(func)


def cache(
    duration: datetime.timedelta | float, *, maxsize: int | None = None
) -> Callable[[Callable[P, R]], Memoized[P, R]]:
    """Cache results for ``duration``; an alias of ``memoize(ttl=duration, maxsize=maxsize)``.

    Kept for v0.1 compatibility. Prefer ``memoize(ttl=...)`` in new code; note this name
    also clashes with `functools.cache`. Every decorated function gets its own
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

_RETRY: Any = _Tag("retry")


class _Flight:
    """An async `once` run in progress, shared by every thread and event loop.

    ``shared`` belongs to the owning loop (callers on that loop await it, so they see
    the task's own outcome, cancellation included); ``done`` is a thread-level future
    that callers on other loops await through `asyncio.wrap_future`. A cancelled run
    resolves ``done`` with ``_RETRY`` so those callers start a new run themselves.
    """

    __slots__ = ("done", "loop", "shared")

    def __init__(self, loop: asyncio.AbstractEventLoop, shared: asyncio.Future[Any]) -> None:
        self.loop = loop
        self.shared = shared
        self.done: concurrent.futures.Future[Any] = concurrent.futures.Future()

    def publish(self, task: asyncio.Future[Any] | None) -> None:
        if self.done.done():
            return
        if task is None or task.cancelled():
            self.abandon()
        elif (exc := task.exception()) is not None:
            self.done.set_exception(exc)
        else:
            self.done.set_result(task.result())

    def abandon(self) -> None:
        if not self.done.done():
            self.done.set_result(_RETRY)


@overload
def once(func: Callable[P, R], /) -> Once[P, R]: ...
@overload
def once(func: None = None, /) -> Callable[[Callable[P, R]], Once[P, R]]: ...
def once(func: Callable[..., Any] | None = None, /) -> Any:
    """Run the function a single time; later calls return the first result.

    Arguments of later calls are ignored. Thread-safe (double-checked lock) and
    async-safe: concurrent first awaits share one run, even across threads that each
    run their own event loop (it runs once per process). If the first call raises, the
    function is not marked done and the next call tries again. The wrapper has
    ``.reset()`` and a ``.called`` flag. While ``python_wrap.testing.disable_caching()``
    is active it runs the function on every call and stores nothing.

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
        # "flight" is the run in progress (shared by every thread and event loop).
        state: dict[str, Any] = {"done": False, "result": None, "gen": 0, "flight": None}

        if _core.is_async(f):

            def finish(flight: _Flight, task: asyncio.Future[Any] | None) -> None:
                with lock:
                    if state["flight"] is flight:
                        state["flight"] = None
                    flight.publish(task)  # under the lock: abandon() may race from a thread

            @functools.wraps(f)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                if _core.overrides.disable_caching:
                    return await f(*args, **kwargs)
                while True:
                    if state["done"]:
                        return state["result"]
                    with lock:
                        if state["done"]:
                            return state["result"]
                        loop = asyncio.get_running_loop()
                        flight: _Flight | None = state["flight"]
                        if flight is not None and flight.loop.is_closed():
                            flight.abandon()  # its loop died: waiters elsewhere retry
                            flight = None
                        owner = flight is None
                        if flight is None:
                            flight = state["flight"] = _Flight(loop, _shared_future())
                            gen = state["gen"]
                    if owner:

                        async def run(gen: int = gen) -> Any:
                            result = await f(*args, **kwargs)
                            with lock:
                                if gen == state["gen"]:  # reset() did not run meanwhile
                                    state.update(done=True, result=result)
                                    wrapper.called = True
                            return result

                        _start(run(), flight.shared, functools.partial(finish, flight))
                    if flight.loop is loop:
                        return await _await_shared(flight.shared)
                    # Another event loop runs it: wait on the thread-level future.
                    result = await _await_shared(asyncio.wrap_future(flight.done))
                    if result is not _RETRY:
                        return result

            wrapper: Any = awrapper
        else:

            @functools.wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                if _core.overrides.disable_caching:
                    return f(*args, **kwargs)
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
                state.update(done=False, result=None, gen=state["gen"] + 1, flight=None)
                wrapper.called = False

        wrapper.reset = reset
        return wrapper

    return decorate if func is None else decorate(func)


# ------------------------------------------------------------------ cached_property

_STATE_KEY = "_python_wrap_cached_property_state"


def _new_state() -> _PropState:
    return _PropState(0)


class _PropState:
    """Per-instance bookkeeping for every `cached_property` of that instance.

    Lives in the instance ``__dict__``. It remembers ``id(owner)`` so a shallow copy of the
    instance (which shares this object) notices and starts with its own state, and it
    pickles as an empty state so locks never end up in a pickle.
    """

    __slots__ = ("entries", "gens", "locks", "owner_id", "tasks")

    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id
        self.entries: dict[str, tuple[Any, float | None]] = {}
        self.locks: dict[str, threading.RLock] = {}
        self.gens: dict[str, int] = {}
        self.tasks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, asyncio.Future[Any]]
        ] = weakref.WeakKeyDictionary()

    def __reduce__(self) -> tuple[Callable[[], _PropState], tuple[()]]:
        return (_new_state, ())

    def lock_for(self, name: str) -> threading.RLock:
        lock = self.locks.get(name)
        if lock is None:
            lock = self.locks.setdefault(name, threading.RLock())  # atomic: one winner
        return lock

    def drop(self, name: str) -> bool:
        with self.lock_for(name):
            self.gens[name] = self.gens.get(name, 0) + 1  # discards results still in flight
            for per_loop in tuple(self.tasks.values()):
                per_loop.pop(name, None)
            return self.entries.pop(name, None) is not None


def _state_of(instance: object, *, create: bool = True) -> _PropState | None:
    try:
        d = instance.__dict__
    except AttributeError:
        raise TypeError(
            f"cached_property needs an instance __dict__, but {type(instance).__name__!r} "
            "has none (a __slots__ class must also list '__dict__')"
        ) from None
    state: _PropState | None = d.get(_STATE_KEY)
    if state is None or state.owner_id != id(instance):
        if not create:
            return None
        state = d.setdefault(_STATE_KEY, _PropState(id(instance)))
        if state.owner_id != id(instance):  # a copy still holding its original's state
            state = d[_STATE_KEY] = _PropState(id(instance))
    return state


class CachedProperty(Generic[T]):
    """The descriptor returned by `cached_property`; ``obj.prop`` has type ``T``.

    For an ``async def`` getter ``T`` is ``Awaitable[X]``: ``await obj.prop`` gives ``X``.
    Instances are read-only data descriptors: assigning to the attribute raises
    ``AttributeError``; ``del obj.prop`` invalidates the cached value.
    """

    def __init__(self, func: Callable[[Any], Any], ttl: float | datetime.timedelta | None) -> None:
        self.func = func
        self.ttl = _ttl_seconds(ttl)
        self.name = func.__name__
        self._named = False
        self.is_async = _core.is_async(func)
        self.__doc__ = func.__doc__
        self.__wrapped__ = func

    def __set_name__(self, owner: type, name: str) -> None:
        if self._named and name != self.name:
            raise TypeError(
                f"cannot use one cached_property under two names ({self.name!r} and {name!r})"
            )
        self.name = name
        self._named = True

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> CachedProperty[T]: ...
    @overload
    def __get__(self, instance: object, owner: type | None = None) -> T: ...
    def __get__(self, instance: object | None, owner: type | None = None) -> Any:
        if instance is None:
            return self
        if _core.overrides.disable_caching:
            return self.func(instance)
        state = _state_of(instance)
        assert state is not None
        if self.is_async:
            return self._aget(instance, state)
        entry = state.entries.get(self.name)
        if entry is not None and (entry[1] is None or _core.monotonic() < entry[1]):
            return entry[0]
        with state.lock_for(self.name):
            entry = state.entries.get(self.name)
            if entry is not None and (entry[1] is None or _core.monotonic() < entry[1]):
                return entry[0]
            gen = state.gens.get(self.name, 0)
            value = self.func(instance)
            if state.gens.get(self.name, 0) == gen:  # not invalidated by the getter itself
                self._store(state, value)
            return value

    def _store(self, state: _PropState, value: Any) -> None:
        expires = None if self.ttl is None else _core.monotonic() + self.ttl
        state.entries[self.name] = (value, expires)

    async def _aget(self, instance: object, state: _PropState) -> Any:
        name = self.name
        lock = state.lock_for(name)
        with lock:
            entry = state.entries.get(name)
            if entry is not None and (entry[1] is None or _core.monotonic() < entry[1]):
                return entry[0]
            per_loop = state.tasks.setdefault(asyncio.get_running_loop(), {})
            shared = per_loop.get(name)
            owner = shared is None
            if shared is None:
                shared = per_loop[name] = _shared_future()
                gen = state.gens.get(name, 0)
        if owner:

            async def run() -> Any:
                value = await self.func(instance)
                with lock:
                    if state.gens.get(name, 0) == gen:  # not invalidated meanwhile
                        self._store(state, value)
                return value

            def done(_t: asyncio.Future[Any] | None) -> None:
                with lock:
                    if per_loop.get(name) is shared:
                        del per_loop[name]

            _start(run(), shared, done)
        return await _await_shared(shared)

    def __delete__(self, instance: object) -> None:
        state = _state_of(instance, create=False)
        if state is not None:
            state.drop(self.name)


class _CachedPropertyDecorator(Protocol):
    @overload
    def __call__(self, func: Callable[[Any], Awaitable[T]], /) -> CachedProperty[Awaitable[T]]: ...
    @overload
    def __call__(self, func: Callable[[Any], T], /) -> CachedProperty[T]: ...


@overload
def cached_property(func: Callable[[Any], Awaitable[T]], /) -> CachedProperty[Awaitable[T]]: ...
@overload
def cached_property(func: Callable[[Any], T], /) -> CachedProperty[T]: ...
@overload
def cached_property(
    func: None = None, /, *, ttl: float | datetime.timedelta | None = None
) -> _CachedPropertyDecorator: ...
def cached_property(
    func: Callable[[Any], Any] | None = None,
    /,
    *,
    ttl: float | datetime.timedelta | None = None,
) -> Any:
    """Compute an attribute once per instance and cache it, like `functools.cached_property`.

    Differences from the standard library version:

    * ``ttl`` makes the value expire (seconds or a ``timedelta``, read through the
      package clock so ``fake_clock`` works).
    * Thread-safe without a global lock: each instance and property has its own lock,
      created lazily, so a slow getter blocks only other readers of the same value.
    * ``async def`` getters work: ``await obj.prop``. Concurrent awaits on one instance
      share a single task per event loop, and a failure is not cached.
    * Explicit invalidation: ``del obj.prop`` or `invalidate` ``(obj, "prop")``.
      The attribute is read-only otherwise.
    * Needs an instance ``__dict__``; on a ``__slots__`` class without one, access raises
      ``TypeError``. Values are kept in a hidden ``__dict__`` entry that copies and
      pickles do not carry over (a copy recomputes).

    With ``python_wrap.testing.disable_caching()`` active the getter runs on every access.

    Args:
        func: The getter (when used bare).
        ttl: Seconds (or a ``timedelta``) the value stays valid. None never expires.

    Returns:
        A `CachedProperty` descriptor, or a decorator when called with options.

    Raises:
        ValueError: If ``ttl`` is not positive.
        TypeError: On access, if the instance has no ``__dict__``.

    Example:
        >>> class Report:
        ...     @cached_property(ttl=60)
        ...     def totals(self):
        ...         return sum(range(10))
        >>> r = Report()
        >>> r.totals
        45
        >>> del r.totals  # next access recomputes
    """

    _ttl_seconds(ttl)  # fail early, at decoration

    def decorate(f: Callable[[Any], Any]) -> CachedProperty[Any]:
        return CachedProperty(f, ttl)

    return decorate if func is None else decorate(func)


def invalidate(instance: object, *names: str) -> int:
    """Forget cached `cached_property` values of ``instance``.

    Args:
        instance: The object that owns the properties.
        *names: Property names to invalidate. With none given, every cached property of
            the instance is invalidated.

    Returns:
        The number of cached values that were dropped.

    Raises:
        AttributeError: If a name is not a `cached_property` of the instance's class.
        TypeError: If the instance has no ``__dict__``.

    Example:
        >>> class C:
        ...     @cached_property
        ...     def x(self):
        ...         return 1
        >>> c = C(); c.x
        1
        >>> invalidate(c, "x")
        1
    """
    for name in names:
        for klass in type(instance).__mro__:
            attr = klass.__dict__.get(name)
            if attr is not None:
                if isinstance(attr, CachedProperty):
                    break
                raise AttributeError(f"{name!r} is not a cached_property")
        else:
            raise AttributeError(f"{type(instance).__name__!r} has no cached_property {name!r}")
    state = _state_of(instance, create=False)
    if state is None:
        return 0
    if names:
        targets = names
    else:
        targets = tuple(
            set(state.entries).union(*(set(per_loop) for per_loop in tuple(state.tasks.values())))
        )
    return sum(state.drop(n) for n in targets)
