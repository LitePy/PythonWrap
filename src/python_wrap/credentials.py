"""Expiring credential caching with one refresh per key and explicit invalidation."""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Coroutine
from typing import Any, Concatenate, ParamSpec, Protocol, TypeVar, cast, overload

from python_wrap import _core, _fork
from python_wrap._runtime import seconds
from python_wrap.caching import CacheInfo, memoize
from python_wrap.concurrency import singleflight

__all__ = ["Expiring", "expiring"]
P = ParamSpec("P")
P2 = ParamSpec("P2")
R = TypeVar("R")
R_co = TypeVar("R_co", covariant=True)
T = TypeVar("T")


class Expiring(Protocol[P, R_co]):
    """A credential-returning callable with invalidate and cache_info controls."""

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R_co: ...

    def invalidate(self) -> None:
        """Invalidate cached values; later calls cannot join an older refresh."""

    def cache_info(self) -> CacheInfo:
        """Return statistics for the credential cache."""

    @overload
    def __get__(
        self: Expiring[Concatenate[T, P2], R_co],
        instance: T,
        owner: type[T] | None = None,
    ) -> Expiring[P2, R_co]: ...

    @overload
    def __get__(self, instance: None, owner: type[Any] | None = None) -> Expiring[P, R_co]: ...


class _ExpiringDecorator(Protocol):
    @overload
    def __call__(
        self, func: Callable[P, Coroutine[Any, Any, tuple[R, float]]]
    ) -> Expiring[P, Coroutine[Any, Any, R]]: ...

    @overload
    def __call__(self, func: Callable[P, tuple[R, float]]) -> Expiring[P, R]: ...


def expiring(*, refresh_margin: float = 60) -> _ExpiringDecorator:
    """Cache a loader returning (value, expires_in_seconds), returning just its value.

    Refreshes are coalesced across callers. Expiry is relative to successful loading,
    reduced by refresh_margin. `.invalidate()` forces the next call to refresh, even
    when a refresh that started before invalidation is still running. Old callers may
    receive their old refresh's result, but it cannot populate the new cache generation.
    For credentials whose lifetime is shorter than the margin no value is cached.
    Sync and async loaders are supported; failures are not cached. Arguments follow
    memoize's hashability rules, including `self` for methods.
    """
    margin = seconds(refresh_margin, positive=False)

    def decorate(func: Callable[..., Any]) -> Any:
        generation = 0
        lock = _fork.ForkSafeLock()
        signature = inspect.signature(func)

        def normalize(
            args: tuple[Any, ...], kw: dict[str, Any]
        ) -> tuple[tuple[Any, ...], dict[str, Any]]:
            bound = signature.bind(*args, **kw)
            bound.apply_defaults()
            return bound.args, bound.kwargs

        def ttl(result: Any) -> float:
            if not isinstance(result, tuple) or len(result) != 2:
                raise TypeError("expiring loaders must return (value, expires_in_seconds)")
            return max(0, seconds(result[1], positive=False) - margin)

        if _core.is_async(func):
            # The generation is part of the in-flight key, so invalidate is a barrier.
            async def load(version: int, /, *args: Any, **kw: Any) -> Any:
                return await func(*args, **kw)

            cached: Any = memoize(ttl=ttl)(singleflight(load))

            @functools.wraps(func)
            async def aw(*args: Any, **kw: Any) -> Any:
                with lock:
                    version = generation
                args, kw = normalize(args, kw)
                return (await cached(version, *args, **kw))[0]

            wrapper: Any = aw
        else:

            def load_sync(version: int, /, *args: Any, **kw: Any) -> Any:
                return func(*args, **kw)

            cached = memoize(ttl=ttl)(singleflight(load_sync))

            @functools.wraps(func)
            def sw(*args: Any, **kw: Any) -> Any:
                with lock:
                    version = generation
                args, kw = normalize(args, kw)
                return cached(version, *args, **kw)[0]

            wrapper = sw

        def invalidate() -> None:
            nonlocal generation
            with lock:
                generation += 1
                cached.cache_clear()

        wrapper.__dict__["invalidate"] = invalidate
        wrapper.__dict__["cache_info"] = cached.cache_info
        return wrapper

    return cast(_ExpiringDecorator, decorate)
