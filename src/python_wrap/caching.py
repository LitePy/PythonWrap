"""Caching decorators: `memoize`, `cache`, `once` and `cached_property`."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import datetime
import functools
import inspect
import threading
import weakref
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine, Hashable
from typing import (
    Any,
    Concatenate,
    Generic,
    NamedTuple,
    ParamSpec,
    Protocol,
    TypeVar,
    overload,
    runtime_checkable,
)

from python_wrap import _core, _fork

__all__ = [
    "CacheInfo",
    "CacheStore",
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
    currsize: int | None


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

    # The three lookups below take the full argument list of the decorated function.
    # On a method that includes the instance (``obj.method.cache_get(obj, x)``):
    # ``obj.method`` is a plain bound method, so its attributes are not bound.

    def cache_delete(self, *args: Any, **kwargs: Any) -> bool:
        """Drop the entry for these arguments. Returns True if one existed."""

    def cache_contains(self, *args: Any, **kwargs: Any) -> bool:
        """True if a live entry exists for these arguments (nothing is computed or counted)."""

    def cache_get(self, *args: Any, **kwargs: Any) -> Any:
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
_KWARGS = _Tag("kwargs")  # separates keyword arguments in keys built without a signature


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
    func: Callable[..., Any],
    typed: bool,
    key: Callable[..., Hashable] | None,
    skip_first: bool = False,
) -> Callable[[tuple[Any, ...], dict[str, Any]], Hashable]:
    """Build cache keys; ``skip_first`` leaves the first argument (``self``) out."""
    if key is not None:
        user_key = key

        def custom(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Hashable:
            return user_key(*args, **kwargs)

        return custom

    try:
        sig: inspect.Signature | None = inspect.signature(func)
    except (TypeError, ValueError):
        sig = None

    skip = 1 if skip_first else 0

    def build(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Hashable:
        if sig is not None:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            named = list(bound.arguments.items())[skip:]
            frozen = tuple(_freeze_arg(n, v) for n, v in named)
        else:
            named = [(f"#{i}", v) for i, v in enumerate(args)][skip:]
            frozen = tuple(_freeze_arg(n, v) for n, v in named)
            if kwargs:
                # Keep the names (and a marker), or f(1, b=2) would equal f(1, 2).
                items = sorted(kwargs.items())
                named += items
                frozen += (_KWARGS, *((n, _freeze_arg(n, v)) for n, v in items))
        values = [v for _, v in named]
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
) -> asyncio.Future[Any]:
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
    return task


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


# fetch() returns a stored entry, ``None`` (absent) or this falsy marker (the entry had
# expired and was just removed). An entry is ``(value, expires, is_exception, fresh_until)``:
# ``expires`` is when it is dropped (None: never), ``fresh_until`` when it turns stale
# (equal to ``expires`` unless ``stale_ttl`` is set).
_EXPIRED: Any = ()

# External-store deadlines use the live Unix wall clock; fake_clock patches it too.


@runtime_checkable
class CacheStore(Protocol):
    """Where `memoize` keeps its entries when given ``store=...``.

    Implement these five methods on any object (no base class needed):

    * ``get(key)`` returns the entry stored under ``key``, or ``None`` if there is none.
    * ``set(key, entry, ttl)`` stores ``entry``. ``ttl`` is the number of seconds after
      which the entry is useless and may be discarded (``None``: keep it until deleted).
    * ``delete(key)`` removes the entry and returns True if there was one.
    * ``clear()`` removes every entry.
    * ``__len__()`` is the number of stored entries (``cache_info().currsize``).
      Network adapters may additionally expose ``size() -> int | None``; memoize uses
      that method when present. Return None for an unknown count rather than scanning
      a remote database on every stats read. ``__len__`` must still return a nonnegative
      integer for normal Python compatibility.

    Contract:

    * Entries are opaque: return exactly what was stored (a store that serialises, such
      as a Redis adapter, must round-trip them, e.g. with `pickle`). They are never None.
      They hold their own deadlines, as Unix timestamps, and `memoize` checks them, so a
      store may forget entries at any time (that is just a miss) and need not expire them.
    * Keys are hashable: normally tuples of the (normalised) arguments, or whatever
      ``key=`` returns. They are **not** strings, so a store that needs strings or bytes
      must serialise them itself (``repr`` or ``pickle`` plus a per-function prefix).
    * The store must be thread-safe: `memoize` does not hold its lock while calling it,
      so a slow store (a network round trip) never blocks other cached functions. Plain
      ``dict`` operations are atomic in CPython, so a dict-backed store qualifies.
    * Methods are synchronous; for ``async def`` functions they run on the event loop,
      so keep them fast.
    * Exceptions raised by the store propagate to the caller.

    Example:
        >>> class DictStore:
        ...     def __init__(self): self.data = {}
        ...     def get(self, key): return self.data.get(key)
        ...     def set(self, key, entry, ttl): self.data[key] = entry
        ...     def delete(self, key): return self.data.pop(key, None) is not None
        ...     def clear(self): self.data.clear()
        ...     def __len__(self): return len(self.data)
        >>> @memoize(store=DictStore())
        ... def double(n):
        ...     return n * 2
        >>> double(2)
        4
    """

    def get(self, key: Hashable, /) -> Any:
        """Return the entry stored under ``key``, or None."""

    def set(self, key: Hashable, entry: Any, ttl: float | None, /) -> None:
        """Store ``entry`` under ``key``; it may be discarded after ``ttl`` seconds."""

    def delete(self, key: Hashable, /) -> bool:
        """Remove the entry under ``key``; True if there was one."""

    def clear(self) -> None:
        """Remove every entry."""

    def __len__(self) -> int:
        """The number of stored entries."""


class _LocalStore:
    """The built-in store: an ordered dict with an optional LRU limit and TTL sweeps.

    Not thread-safe by itself: the decorator holds its lock around every call.
    """

    __slots__ = ("data", "expiring", "inserts", "live", "maxsize")

    def __init__(self, maxsize: int | None, expiring: bool) -> None:
        self.data: OrderedDict[Hashable, tuple[Any, float | None, bool, float | None]] = (
            OrderedDict()
        )
        self.maxsize = maxsize
        self.expiring = expiring
        self.inserts = 0
        self.live = 0

    def get(self, k: Hashable) -> Any:
        return self.data.get(k)

    def delete(self, k: Hashable) -> bool:
        return self.data.pop(k, _MISSING) is not _MISSING

    def clear(self) -> None:
        self.data.clear()
        self.inserts = self.live = 0

    def __len__(self) -> int:
        return len(self.data)

    def sweep(self) -> int:
        """Drop every expired entry; returns how many."""
        now = _core.monotonic()
        data = self.data
        dead = [k for k, e in data.items() if e[1] is not None and now >= e[1]]
        for k in dead:
            del data[k]
        return len(dead)

    def put(self, k: Hashable, entry: tuple[Any, float | None, bool, float | None]) -> int:
        """Store an entry; returns ``ttl_evicted * 2**32 + lru_evicted`` (cheap to pass)."""
        data = self.data
        data[k] = entry
        ttl_evicted = lru_evicted = 0
        if self.maxsize is not None:
            data.move_to_end(k)
        if self.expiring:
            # Amortised O(1): a full sweep only after at least
            # max(64, entries left by the previous sweep) inserts.
            self.inserts += 1
            if self.inserts >= max(64, self.live):
                ttl_evicted = self.sweep()
                self.inserts, self.live = 0, len(data)
        if self.maxsize is not None:
            while len(data) > self.maxsize:
                data.popitem(last=False)
                lru_evicted += 1
        return (ttl_evicted << 32) | lru_evicted


@overload
def memoize(func: Callable[P, R], /) -> Memoized[P, R]: ...
@overload
def memoize(
    func: None = None,
    /,
    *,
    maxsize: int | None = None,
    ttl: float | datetime.timedelta | Callable[[Any], float | None] | None = None,
    stale_ttl: float | datetime.timedelta | None = None,
    typed: bool = False,
    key: Callable[..., Hashable] | None = None,
    cache_exceptions: type[BaseException] | tuple[type[BaseException], ...] = (),
    exception_ttl: float | datetime.timedelta | None = None,
    store: CacheStore | None = None,
    per_instance: bool = False,
    logger: _core.LoggerLike = None,
) -> Callable[[Callable[P, R]], Memoized[P, R]]: ...
def memoize(
    func: Callable[..., Any] | None = None,
    /,
    *,
    maxsize: int | None = None,
    ttl: float | datetime.timedelta | Callable[[Any], float | None] | None = None,
    stale_ttl: float | datetime.timedelta | None = None,
    typed: bool = False,
    key: Callable[..., Hashable] | None = None,
    cache_exceptions: type[BaseException] | tuple[type[BaseException], ...] = (),
    exception_ttl: float | datetime.timedelta | None = None,
    store: CacheStore | None = None,
    per_instance: bool = False,
    logger: _core.LoggerLike = None,
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

    Stale-while-revalidate: with ``stale_ttl`` an entry older than ``ttl`` is *stale* but
    still served, immediately, for ``stale_ttl`` more seconds; the call that finds it
    stale starts one background refresh (``async def``: a task on the running event
    loop, shared with concurrent misses; sync: a daemon thread running in a copy of the
    caller's `contextvars` context, at most one per key). A failed refresh keeps the
    stale value, is logged at WARNING and lets the next call try again. Cached
    exceptions are never served stale.

    Methods: ``self`` is part of the key, so results are per instance, and the cache
    holds a strong reference to every instance it has seen, keeping it alive. A class
    whose instances are unhashable (for example an ``eq=True`` dataclass) fails with a
    ``TypeError`` naming ``self``. ``per_instance=True`` solves both: each instance gets
    its own cache, found by identity through a weak reference, so instances stay
    collectable (their entries go with them) and need not be hashable. They must be
    weak-referenceable (a ``__slots__`` class must list ``'__weakref__'``), and a cached
    result that references its instance keeps it alive.

    The wrapper has ``cache_clear()``, ``cache_info()`` (a `CacheInfo`),
    ``cache_delete(*args, **kwargs)``, ``cache_contains(*args, **kwargs)``,
    ``cache_get(*args, **kwargs)`` and ``cache_parameters()``. ``cache_contains`` and
    ``cache_get`` look an entry up without computing, counting a hit or miss, or
    refreshing its LRU position. All three take the decorated function's full argument
    list, so on a method pass the instance too: ``obj.method.cache_get(obj, x)``.

    While ``python_wrap.testing.disable_caching()`` is active the cache is neither read
    nor written. Listeners registered with `python_wrap.events` receive ``cache.hit``,
    ``cache.miss``, ``cache.evicted`` (field ``reason``: ``"lru"`` or ``"ttl"``),
    ``cache.stale`` (a stale value was served and a refresh started) and
    ``cache.refresh_failed`` (field ``exception``).

    Args:
        func: The function (when used bare).
        maxsize: Maximum entries; least recently used are evicted. None is unbounded.
            With ``per_instance`` the limit applies to each instance.
        ttl: Seconds (or a ``timedelta``) an entry stays valid. None never expires. May
            also be a callable ``ttl(result) -> float | None`` evaluated when a result
            is stored (None: never expires; zero or negative: not stored).
        stale_ttl: Seconds (or a ``timedelta``) after ``ttl`` during which the stale
            value is still served while a background refresh runs. Requires ``ttl``.
        typed: If True, ``f(1)`` and ``f(1.0)`` are cached separately.
        key: Custom ``key(*args, **kwargs)`` returning a hashable cache key. It receives
            every argument (``self`` too; with ``per_instance`` it should leave it out).
        cache_exceptions: Exception class (or tuple of classes) whose instances are
            cached and re-raised like results. ``asyncio.CancelledError`` never is.
        exception_ttl: Seconds a cached exception stays valid. Defaults to ``ttl``; it
            must be given when ``ttl`` is a callable and ``cache_exceptions`` is set.
        store: A `CacheStore` to keep entries in instead of the built-in LRU dict.
        per_instance: For methods: one cache per instance, held through a weak
            reference, keyed by the arguments after ``self``.
        logger: Logger (or name) for failed background refreshes.

    Returns:
        The wrapped function, or a decorator when called with options.

    Raises:
        ValueError: If ``maxsize`` is below 1, a ttl is not positive, or ``stale_ttl``
            is given without ``ttl``.
        TypeError: For a non-exception in ``cache_exceptions``, an object that is not a
            `CacheStore`, ``store`` combined with ``maxsize`` or ``per_instance``, or
            ``per_instance`` on a function without parameters. At call time, for an
            unhashable argument (see above) or, with ``per_instance``, an instance that
            cannot be weakly referenced.

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
    if store is not None:
        if maxsize is not None:
            raise TypeError("maxsize only applies to the built-in store; not with store=")
        if per_instance:
            raise TypeError("per_instance=True uses the built-in store; not with store=")
        if not isinstance(store, CacheStore):
            raise TypeError("store must implement CacheStore: get, set, delete, clear and __len__")
    ttl_fn: Callable[[Any], float | None] | None = None
    ttl_s: float | None = None
    if callable(ttl):
        ttl_fn = ttl
    else:
        ttl_s = _ttl_seconds(ttl)
    if stale_ttl is not None and ttl is None:
        raise ValueError("stale_ttl requires ttl")
    stale_s = _ttl_seconds(stale_ttl)
    exc_types = _exception_types(cache_exceptions)
    exc_ttl_s = _ttl_seconds(exception_ttl) if exception_ttl is not None else ttl_s
    if exc_types and ttl_fn is not None and exception_ttl is None:
        raise ValueError("exception_ttl is required when ttl is a callable")
    lru = maxsize is not None
    expiring = ttl_s is not None or ttl_fn is not None or exc_ttl_s is not None
    builtin = store is None
    ov = _core.overrides

    if builtin:

        def now() -> float:
            return _core.monotonic()
    else:

        def now() -> float:
            return _core.wall_time()

    def decorate(f: Callable[..., Any]) -> Any:
        build_key = _make_key_builder(f, typed, key, skip_first=per_instance)
        fast_n = _fast_arity(f, typed, key)
        if per_instance and fast_n == 0:
            raise TypeError("per_instance=True needs a method (a function taking self)")
        name = _core.qualname(f)
        main: Any = _LocalStore(maxsize, expiring) if builtin else store
        # per_instance: id(instance) -> (weak reference, that instance's store)
        instances: dict[int, tuple[weakref.ref[Any], _LocalStore]] = {}
        # In-flight tasks are per event loop: a task cannot be awaited from another loop.
        inflight: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[Hashable, asyncio.Future[Any]]
        ] = weakref.WeakKeyDictionary()
        refreshing: set[Hashable] = set()  # sync keys being refreshed in a thread
        background: set[asyncio.Future[Any]] = set()  # strong refs to refresh tasks
        lock = _fork.ForkSafeLock()
        guard: Any = lock if builtin else contextlib.nullcontext()
        hits = misses = gen = 0

        def key_of(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Hashable:
            if len(args) == fast_n and not kwargs:
                k = args[1:] if per_instance else args
                try:
                    hash(k)
                except TypeError:
                    pass
                else:
                    return k
            return build_key(args, kwargs)

        def bucket_of(args: tuple[Any, ...], create: bool = True) -> Any:
            if not per_instance:
                return main
            if not args:
                if not create:
                    return None
                raise TypeError(
                    f"memoize(per_instance=True): call {name} with the instance as its "
                    "first positional argument"
                )
            obj = args[0]
            i = id(obj)
            slot = instances.get(i)
            if slot is not None and slot[0]() is obj:
                return slot[1]
            if not create:
                return None

            def forget(ref: weakref.ref[Any], i: int = i) -> None:
                # Runs while the instance is being destroyed, maybe in a thread that holds
                # ``lock``: so no locking here (dict operations are atomic). The id cannot
                # be reused before this callback has run.
                current = instances.get(i)
                if current is not None and current[0] is ref:
                    instances.pop(i, None)

            try:
                ref = weakref.ref(obj, forget)
            except TypeError:
                raise TypeError(
                    f"memoize(per_instance=True) needs weak-referenceable instances, but "
                    f"{type(obj).__name__!r} is not (a __slots__ class must also list "
                    "'__weakref__')"
                ) from None
            with lock:
                slot = instances.get(i)
                if slot is not None and slot[0]() is obj:
                    return slot[1]
                bucket = _LocalStore(maxsize, expiring)
                instances[i] = (ref, bucket)
            return bucket

        def live_buckets() -> list[_LocalStore]:
            # Caller holds the lock. A collected instance may drop its slot meanwhile.
            while True:
                try:
                    slots = list(instances.values())
                except RuntimeError:  # pragma: no cover - changed size during iteration
                    continue
                return [b for ref, b in slots if ref() is not None]

        def fetch(b: Any, k: Hashable) -> Any:
            # Caller holds ``guard``.
            entry = b.get(k)
            if entry is None:
                return None
            expires = entry[1]
            if expires is not None and now() >= expires:
                b.delete(k)
                return _EXPIRED
            if lru:
                b.data.move_to_end(k)
            return entry

        def is_stale(entry: Any) -> bool:
            return entry[1] is not None and not entry[2] and now() >= entry[3]

        def report_expired() -> None:
            _core.emit("cache.evicted", name, reason="ttl")

        def save(b: Any, k: Hashable, value: Any, g: int, is_exc: bool = False) -> None:
            if is_exc:
                seconds = exc_ttl_s
            elif ttl_fn is not None:
                seconds = ttl_fn(value)
                if seconds is not None and seconds <= 0:
                    return  # not worth storing
            else:
                seconds = ttl_s
            keep = seconds
            if seconds is not None and stale_s is not None and not is_exc:
                keep = seconds + stale_s
            evicted = 0
            with guard:
                if g != gen:
                    return  # cache_clear() ran while computing
                t = now()
                fresh = None if seconds is None else t + seconds
                expires = None if keep is None else t + keep
                entry = (value, expires, is_exc, fresh)
                if builtin:
                    evicted = b.put(k, entry)
                else:
                    b.set(k, entry, keep)
            if evicted and _core.listeners:
                for _ in range(evicted >> 32):
                    report_expired()
                for _ in range(evicted & 0xFFFFFFFF):
                    _core.emit("cache.evicted", name, reason="lru")

        def refresh_failed(exc: BaseException) -> None:
            _core.get_logger(logger).warning(
                "memoize: background refresh of %s failed; keeping the stale value",
                name,
                exc_info=exc,
            )
            _core.emit("cache.refresh_failed", name, exception=exc)

        def refresh_in_thread(
            b: Any, k: Hashable, args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> None:
            rk = (id(b), k) if per_instance else k
            with lock:
                if rk in refreshing:
                    return
                refreshing.add(rk)
                g = gen

            def run() -> None:
                try:
                    save(b, k, f(*args, **kwargs), g)
                except Exception as exc:
                    refresh_failed(exc)
                finally:
                    with lock:
                        refreshing.discard(rk)

            thread = threading.Thread(
                target=contextvars.copy_context().run,
                args=(run,),
                name=f"python_wrap-refresh-{name}",
                daemon=True,
            )
            try:
                thread.start()
            except Exception as exc:  # e.g. "can't start new thread": keep serving stale
                with lock:
                    refreshing.discard(rk)
                refresh_failed(exc)
                return
            if _core.listeners:
                _core.emit("cache.stale", name)

        def refresh_in_task(
            b: Any, k: Hashable, args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> None:
            rk = (id(b), k) if per_instance else k
            with lock:
                loop = asyncio.get_running_loop()
                loop_inflight = inflight.setdefault(loop, {})
                if rk in loop_inflight:
                    return  # a refresh (or a miss) for this key is already running
                g = gen
                shared_fut = loop_inflight[rk] = _shared_future()

            async def run() -> Any:
                try:
                    result = await f(*args, **kwargs)
                    save(b, k, result, g)
                except Exception as exc:
                    refresh_failed(exc)
                    raise
                return result

            def done(_t: asyncio.Future[Any] | None) -> None:
                with lock:
                    if loop_inflight.get(rk) is shared_fut:
                        del loop_inflight[rk]

            task = _start(run(), shared_fut, done)
            background.add(task)
            task.add_done_callback(background.discard)
            if _core.listeners:
                _core.emit("cache.stale", name)

        def cache_clear() -> None:
            nonlocal hits, misses, gen
            with lock:
                if builtin:
                    main.clear()
                    instances.clear()
                inflight.clear()
                hits = misses = 0
                gen += 1
            if not builtin:
                main.clear()

        def cache_info() -> CacheInfo:
            expired = 0
            size: int | None = 0
            with lock:
                if per_instance:
                    buckets = live_buckets()
                    for b in buckets:
                        expired += b.sweep()
                    size = sum(len(b) for b in buckets)
                elif builtin:
                    expired = main.sweep()
                    size = len(main)
                info = (hits, misses)
            if not builtin:
                size_fn = getattr(main, "size", None)
                size = size_fn() if callable(size_fn) else len(main)
            if _core.listeners:
                for _ in range(expired):
                    report_expired()
            return CacheInfo(info[0], info[1], maxsize, size)

        def peek(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
            b = bucket_of(args, create=False)
            if b is None:
                return None
            k = key_of(args, kwargs)
            with guard:
                entry = b.get(k)
            if entry is None or (entry[1] is not None and now() >= entry[1]):
                return None
            return entry

        def cache_delete(*args: Any, **kwargs: Any) -> bool:
            b = bucket_of(args, create=False)
            if b is None:
                return False
            k = key_of(args, kwargs)
            with guard:
                return bool(b.delete(k))

        def cache_contains(*args: Any, **kwargs: Any) -> bool:
            return peek(args, kwargs) is not None

        def cache_get(*args: Any, **kwargs: Any) -> Any:
            entry = peek(args, kwargs)
            if entry is None:
                raise KeyError("no cached entry for these arguments")
            if entry[2]:
                raise entry[0]
            return entry[0]

        def cache_parameters() -> dict[str, Any]:
            return {
                "maxsize": maxsize,
                "ttl": ttl_fn if ttl_fn is not None else ttl_s,
                "stale_ttl": stale_s,
                "typed": typed,
                "cache_exceptions": exc_types,
                "exception_ttl": exc_ttl_s,
                "store": store,
                "per_instance": per_instance,
            }

        data: Any = main.data if builtin else None
        data_get: Any = data.get if builtin else None

        def fetch_main(k: Hashable) -> Any:
            # `fetch` for the default store, inlined for speed. Caller holds the lock.
            entry = data_get(k)
            if entry is None:
                return None
            expires = entry[1]
            if expires is not None and _core.monotonic() >= expires:
                del data[k]
                return _EXPIRED
            if lru:
                data.move_to_end(k)
            return entry

        async def amiss(b: Any, k: Hashable, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
            # The async miss path: share one in-flight task per key and event loop.
            nonlocal hits, misses
            rk = (id(b), k) if per_instance else k
            with lock:
                loop = asyncio.get_running_loop()
                loop_inflight = inflight.setdefault(loop, {})
                shared_fut = loop_inflight.get(rk)
                if shared_fut is None:
                    misses += 1
                    g = gen
                    shared = False
                    shared_fut = loop_inflight[rk] = _shared_future()
                else:
                    hits += 1
                    shared = True
            if not shared:

                async def run() -> Any:
                    try:
                        result = await f(*args, **kwargs)
                    except exc_types as exc:
                        if not isinstance(exc, asyncio.CancelledError):
                            save(b, k, exc, g, True)
                        raise
                    save(b, k, result, g)
                    return result

                def done(_t: asyncio.Future[Any] | None) -> None:
                    with lock:
                        if loop_inflight.get(rk) is shared_fut:
                            del loop_inflight[rk]

                _start(run(), shared_fut, done)
            if _core.listeners:
                _core.emit("cache.hit" if shared else "cache.miss", name)
            # one cancelled caller must not cancel the shared computation
            return await _await_shared(shared_fut)

        if _core.is_async(f) and builtin and not per_instance:

            @functools.wraps(f)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                nonlocal hits
                if ov.disable_caching:
                    return await f(*args, **kwargs)
                k = args if len(args) == fast_n and not kwargs else build_key(args, kwargs)
                try:
                    with lock:
                        entry = fetch_main(k)
                        if entry:
                            hits += 1
                except TypeError:
                    if k is not args:
                        raise
                    k = build_key(args, kwargs)
                    with lock:
                        entry = fetch_main(k)
                        if entry:
                            hits += 1
                if entry:
                    if _core.listeners:
                        _core.emit("cache.hit", name)
                    if stale_s is not None and is_stale(entry):
                        refresh_in_task(main, k, args, kwargs)
                    if entry[2]:
                        raise entry[0]
                    return entry[0]
                if _core.listeners and entry is not None:
                    report_expired()
                return await amiss(main, k, args, kwargs)

            wrapper: Any = awrapper
        elif _core.is_async(f):

            @functools.wraps(f)
            async def agwrapper(*args: Any, **kwargs: Any) -> Any:
                nonlocal hits
                if ov.disable_caching:
                    return await f(*args, **kwargs)
                b = bucket_of(args)
                k = key_of(args, kwargs)
                with guard:
                    entry = fetch(b, k)
                if entry:
                    with lock:
                        hits += 1
                    if _core.listeners:
                        _core.emit("cache.hit", name)
                    if stale_s is not None and is_stale(entry):
                        refresh_in_task(b, k, args, kwargs)
                    if entry[2]:
                        raise entry[0]
                    return entry[0]
                if _core.listeners and entry is not None:
                    report_expired()
                return await amiss(b, k, args, kwargs)

            wrapper = agwrapper
        elif builtin and not per_instance:

            @functools.wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                nonlocal hits, misses
                if ov.disable_caching:
                    return f(*args, **kwargs)
                k = args if len(args) == fast_n and not kwargs else build_key(args, kwargs)
                try:
                    with lock:
                        entry = data_get(k)
                        if entry is not None and entry[1] is None:
                            # hot path: a live entry that never expires
                            if lru:
                                data.move_to_end(k)
                            hits += 1
                        else:
                            entry = fetch_main(k)
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
                        entry = fetch_main(k)
                        if entry:
                            hits += 1
                        else:
                            misses += 1
                            g = gen
                if entry:
                    if _core.listeners:
                        _core.emit("cache.hit", name)
                    if stale_s is not None and is_stale(entry):
                        refresh_in_thread(main, k, args, kwargs)
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
                    save(main, k, exc, g, True)
                    raise
                save(main, k, result, g)
                return result

        else:

            @functools.wraps(f)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                nonlocal hits, misses
                if ov.disable_caching:
                    return f(*args, **kwargs)
                b = bucket_of(args)
                k = key_of(args, kwargs)
                with guard:
                    entry = fetch(b, k)
                with lock:
                    if entry:
                        hits += 1
                    else:
                        misses += 1
                    g = gen
                if entry:
                    if _core.listeners:
                        _core.emit("cache.hit", name)
                    if stale_s is not None and is_stale(entry):
                        refresh_in_thread(b, k, args, kwargs)
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
                    save(b, k, exc, g, True)
                    raise
                save(b, k, result, g)
                return result

        wrapper.cache_clear = cache_clear
        wrapper.cache_info = cache_info
        wrapper.cache_delete = cache_delete
        wrapper.cache_contains = cache_contains
        wrapper.cache_get = cache_get

        def reset_after_fork() -> None:
            nonlocal hits, misses, gen
            if builtin:
                main.clear()
                instances.clear()
            inflight.clear()
            refreshing.clear()
            background.clear()
            hits = misses = 0
            gen += 1

        wrapper.__dict__["_fork_reset"] = _fork.register_reset(reset_after_fork)
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
        lock = _fork.ForkSafeRLock()
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

        wrapper.__dict__["_fork_reset"] = _fork.register_reset(reset)
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

    __slots__ = ("__weakref__", "entries", "gens", "locks", "owner_id", "tasks")

    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id
        self.entries: dict[str, tuple[Any, float | None]] = {}
        self.locks: dict[str, _fork.ForkSafeRLock] = {}
        self.gens: dict[str, int] = {}
        self.tasks: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[str, asyncio.Future[Any]]
        ] = weakref.WeakKeyDictionary()
        _fork.register(self)

    def _reset_after_fork(self) -> None:
        self.entries.clear()
        self.tasks.clear()
        self.gens.clear()

    def __reduce__(self) -> tuple[Callable[[], _PropState], tuple[()]]:
        return (_new_state, ())

    def lock_for(self, name: str) -> _fork.ForkSafeRLock:
        lock = self.locks.get(name)
        if lock is None:
            lock = self.locks.setdefault(name, _fork.ForkSafeRLock())  # atomic: one winner
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
