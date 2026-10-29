"""Dynamic kill switches, with explicit sources and bounded local read caching."""

from __future__ import annotations

import functools
import os
from collections.abc import Callable, Coroutine
from typing import Any, Literal, TypeVar, cast, overload

from python_wrap import _core
from python_wrap._runtime import seconds
from python_wrap.caching import memoize
from python_wrap.retry import _Decorator

__all__ = ["EnvSource", "FeatureDisabled", "RedisSource", "switch"]
F = TypeVar("F", bound=Callable[..., Any])
T = TypeVar("T")


def _enabled(value: Any) -> bool:
    if isinstance(value, bytes):
        value = value.decode()
    if isinstance(value, str):
        if value.casefold() in {"1", "true", "yes", "on", "enabled"}:
            return True
        if value.casefold() in {"0", "false", "no", "off", "disabled"}:
            return False
        raise ValueError(f"invalid switch value: {value!r}")
    return bool(value)


class FeatureDisabled(RuntimeError):
    """A disabled integration was called."""


class EnvSource:
    """Read PREFIX + uppercased switch name from the current process environment."""

    def __init__(self, prefix: str = "PW_SWITCH_", *, default: bool = True) -> None:
        self.prefix, self.default = prefix, default

    def __call__(self, name: str) -> bool:
        return _enabled(os.environ.get(self.prefix + name.upper(), self.default))


class RedisSource:
    """Read a boolean flag from a supplied Redis client; backend errors propagate."""

    def __init__(
        self, client: Any, prefix: str = "python-wrap:switch:", *, default: bool = True
    ) -> None:
        self.client, self.prefix, self.default = client, prefix, default

    def __call__(self, name: str) -> bool:
        value = self.client.get(self.prefix + name)
        return self.default if value is None else _enabled(value)


@overload
def switch(
    name: str,
    *,
    source: Callable[[str], bool] | None = None,
    when_off: Literal["raise"] = "raise",
    ttl: float = 0,
) -> Callable[[F], F]: ...
@overload
def switch(
    name: str,
    *,
    source: Callable[[str], bool] | None = None,
    when_off: Literal["skip"],
    ttl: float = 0,
) -> _Decorator[None]: ...
@overload
def switch(
    name: str,
    *,
    source: Callable[[str], bool] | None = None,
    when_off: Callable[..., Coroutine[Any, Any, T]],
    ttl: float = 0,
) -> _Decorator[T]: ...
@overload
def switch(
    name: str,
    *,
    source: Callable[[str], bool] | None = None,
    when_off: Callable[..., T],
    ttl: float = 0,
) -> _Decorator[T]: ...


def switch(
    name: str,
    *,
    source: Callable[[str], bool] | None = None,
    when_off: str | Callable[..., Any] = "raise",
    ttl: float = 0,
) -> Any:
    """Read a switch at call time; disabled calls raise, skip, or use a fallback.

    `when_off="skip"` returns None. `when_off=callable` receives the original arguments.
    Async functions offload synchronous source I/O and await async fallbacks.
    """
    import asyncio
    import inspect

    ttl = seconds(ttl, positive=False)
    if not callable(when_off) and when_off not in {"raise", "skip"}:
        raise ValueError("when_off must be 'raise', 'skip', or a callable")
    read = source if source is not None else EnvSource()
    check: Callable[[str], bool] = memoize(ttl=ttl)(read) if ttl else read

    def decorate(func: F) -> F:
        def off(args: tuple[Any, ...], kw: dict[str, Any]) -> Any:
            if callable(when_off):
                return when_off(*args, **kw)
            if when_off == "raise":
                raise FeatureDisabled(name)
            return None

        if _core.is_async(func):

            @functools.wraps(func)
            async def aw(*args: Any, **kw: Any) -> Any:
                if await asyncio.to_thread(check, name):
                    return await func(*args, **kw)
                value = off(args, kw)
                return await value if inspect.isawaitable(value) else value

            return cast(F, aw)

        if callable(when_off) and _core.is_async(when_off):
            raise TypeError("a synchronous switch cannot await an async fallback")

        @functools.wraps(func)
        def sw(*args: Any, **kw: Any) -> Any:
            if check(name):
                return func(*args, **kw)
            value = off(args, kw)
            if inspect.isawaitable(value):
                if inspect.iscoroutine(value):
                    value.close()
                raise TypeError("a synchronous switch fallback returned an awaitable")
            return value

        return cast(F, sw)

    return decorate
