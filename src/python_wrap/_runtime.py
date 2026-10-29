"""Small shared helpers for optional integrations and backend-based decorators."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import inspect
import math
import pickle
import re
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar


def seconds(value: float | str, *, positive: bool = True) -> float:
    """Parse a finite duration, optionally suffixed by ms/s/m/h/d."""
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*", value)
        if not match:
            raise ValueError(f"invalid duration: {value!r}")
        result = (
            float(match[1])
            * {None: 1, "ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
        )
    else:
        result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise ValueError(
            "duration must be finite and positive"
            if positive
            else "duration must be finite and non-negative"
        )
    return result


def digest(value: Any) -> str:
    return hashlib.sha256(pickle.dumps(value, protocol=4)).hexdigest()


def call_key(
    func: Callable[..., Any],
    key: str | Callable[..., Any] | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> str:
    if isinstance(key, str):
        return key
    if key is not None:
        return str(key(*args, **kwargs))
    bound = inspect.signature(func).bind(*args, **kwargs)
    bound.apply_defaults()
    return f"{func.__module__}.{func.__qualname__}:{digest(tuple(bound.arguments.items()))}"


def preserve(func: Callable[..., Any], wrapper: Callable[..., Any]) -> Any:
    return functools.update_wrapper(wrapper, func)


P = ParamSpec("P")
T = TypeVar("T")


async def offload_settled(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run synchronous side effects in a thread, settling them before cancellation exits.

    Callers must set I/O timeouts on their clients: a Python task cannot stop a thread.
    This prevents a cancelled batch from releasing its outer lease while a checkpoint
    write or synchronous handler is still running.
    """
    import asyncio

    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except BaseException:
            break
    if cancelled is not None:
        with contextlib.suppress(BaseException):
            task.result()
        raise cancelled
    return task.result()
