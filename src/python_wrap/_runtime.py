"""Small shared helpers for optional integrations and backend-based decorators."""

from __future__ import annotations

import contextlib
import dataclasses
import enum
import functools
import hashlib
import inspect
import math
import pickle
import re
from pathlib import PurePath
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
    """Pickle digest for names and other values that are stable by construction."""
    return hashlib.sha256(pickle.dumps(value, protocol=4)).hexdigest()


def _enc(value: Any, out: list[bytes]) -> None:
    kind = type(value)
    if value is None:
        out.append(b"N")
    elif kind is bool:
        out.append(b"T" if value else b"F")
    elif kind is int:
        out.append(b"i%d;" % value)
    elif kind is float:
        out.append(b"f" + value.hex().encode() + b";")
    elif kind is str:
        raw = value.encode("utf-8", "surrogatepass")
        out.append(b"s%d:" % len(raw) + raw)
    elif kind in (bytes, bytearray):
        out.append(b"b%d:" % len(value) + bytes(value))
    elif kind in (tuple, list):
        out.append((b"t%d[" if kind is tuple else b"l%d[") % len(value))
        for item in value:
            _enc(item, out)
    elif kind is dict:
        items = []
        for k, v in value.items():
            part: list[bytes] = []
            _enc(k, part)
            _enc(v, part)
            items.append(b"".join(part))
        out.append(b"d%d{" % len(items) + b"".join(sorted(items)))
    elif kind in (set, frozenset):
        members = []
        for item in value:
            part = []
            _enc(item, part)
            members.append(b"".join(part))
        out.append(b"S%d{" % len(members) + b"".join(sorted(members)))
    elif kind.__name__ == "_Tag":  # caching's list/dict/set markers
        out.append(b"g" + value.name.encode() + b";")
    elif isinstance(value, enum.Enum):
        out.append(f"E{kind.__module__}.{kind.__qualname__}:".encode())
        _enc(value.value, out)
    elif kind.__module__ in ("datetime", "decimal", "uuid") or isinstance(value, PurePath):
        out.append(f"O{kind.__qualname__}:{value}".encode() + b";")
    elif dataclasses.is_dataclass(value) and not isinstance(value, type):
        out.append(f"C{kind.__module__}.{kind.__qualname__}:".encode())
        _enc({f.name: getattr(value, f.name) for f in dataclasses.fields(value)}, out)
    else:
        raise TypeError(
            f"a value of type {kind.__qualname__!r} has no stable cache key across "
            "processes; pass key=<callable> that returns a str or tuple of plain values"
        )


def stable_digest(value: Any) -> str:
    """A digest that is equal in every process for equal plain values.

    Dict items and set members are sorted by their encoding, so insertion order and the
    per-process hash seed never change the result. Unsupported objects raise `TypeError`.
    """
    out: list[bytes] = []
    _enc(value, out)
    return hashlib.sha256(b"".join(out)).hexdigest()


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
    for name, value in bound.arguments.items():
        try:
            _enc(value, [])
        except TypeError as exc:
            raise TypeError(f"argument {name!r}: {exc}") from None
    return f"{func.__module__}.{func.__qualname__}:{stable_digest(tuple(bound.arguments.items()))}"


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
