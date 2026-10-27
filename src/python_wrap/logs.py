"""Logging decorators: argument/return logging, audit records and call-stack tracing.

All of them log through :func:`python_wrap._core.get_logger`, never configure logging,
and support plain functions, ``async def`` functions and methods.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import reprlib
import sys
import traceback
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import FrameType
from typing import Any, TypeVar, cast, overload

from python_wrap import _core
from python_wrap._core import LoggerLike, P, R

__all__ = [
    "DEFAULT_REDACT",
    "AuditRecord",
    "audit",
    "log_args",
    "log_return",
    "trace",
]

S = TypeVar("S")

DEFAULT_REDACT: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "private_key",
    "cookie",
    "session",
)

_MASK = "***"
_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- helpers


def _safe_repr(value: object) -> str:
    """``repr(value)`` that never raises."""
    try:
        return repr(value)
    except BaseException:
        return f"<unrepresentable {type(value).__name__}>"


def _instrument(
    func: Callable[P, R],
    start: Callable[[tuple[Any, ...], dict[str, Any]], S],
    finish: Callable[[S, bool, Any], None],
) -> Callable[P, R]:
    """Wrap ``func`` (sync or async) with a start hook and a finish hook.

    ``start(args, kwargs)`` runs before the call and returns a state object.
    ``finish(state, ok, value)`` runs afterwards with the return value (``ok=True``) or the
    raised exception (``ok=False``); the exception is then re-raised. For ``async def``
    functions the hooks surround the awaited coroutine. A failing ``finish`` is logged and
    swallowed: it never replaces the function's result or exception.
    """

    def run_finish(state: S, ok: bool, value: Any) -> None:
        try:
            finish(state, ok, value)
        except Exception:
            _core.get_logger(None).exception("%s: logging hook failed", _core.qualname(func))

    if _core.is_async(func):

        @functools.wraps(func)
        async def awrapper(*args: Any, **kwargs: Any) -> Any:
            state = start(args, kwargs)
            try:
                result = await func(*args, **kwargs)  # type: ignore[misc]
            except BaseException as exc:
                run_finish(state, False, exc)
                raise
            run_finish(state, True, result)
            return result

        return cast("Callable[P, R]", awrapper)

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        state = start(args, kwargs)
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            run_finish(state, False, exc)
            raise
        run_finish(state, True, result)
        return result

    return cast("Callable[P, R]", wrapper)


def _redact_predicate(redact: Iterable[str] | None) -> Callable[[str], bool]:
    """Build a case-insensitive "does this name look sensitive" test."""
    needles = tuple(n.lower() for n in (DEFAULT_REDACT if redact is None else redact) if n)
    if not needles:
        return lambda name: False
    return lambda name: any(n in name.lower() for n in needles)


def _short(value: object, max_length: int) -> str:
    """``repr`` with container truncation and a hard character limit."""
    r = reprlib.Repr()
    r.maxstring = r.maxother = max_length
    r.maxlist = r.maxtuple = r.maxset = r.maxdict = r.maxdeque = r.maxarray = 6
    text = r.repr(value)  # reprlib already survives a failing __repr__
    if max_length >= 0 and len(text) > max_length:
        text = text[: max(max_length - 3, 0)] + "..."
    return text


def _bind_arguments(
    sig: inspect.Signature | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    is_secret: Callable[[str], bool],
) -> dict[str, Any]:
    """Map every passed argument to its parameter name, masking sensitive ones."""
    named: dict[str, Any] = {}
    bound = None
    if sig is not None:
        try:
            bound = sig.bind(*args, **kwargs)
        except TypeError:
            bound = None
    if sig is None or bound is None:  # the call is invalid anyway; show what we were given
        named = {f"arg{i}": v for i, v in enumerate(args)}
        named.update(kwargs)
    else:
        for name, value in bound.arguments.items():
            if sig.parameters[name].kind is inspect.Parameter.VAR_KEYWORD:
                named.update(value)
            else:
                named[name] = value
    return {k: (_MASK if is_secret(k) else v) for k, v in named.items()}


def _signature(func: Callable[..., Any]) -> inspect.Signature | None:
    try:
        return inspect.signature(func)
    except (TypeError, ValueError):
        return None


def _format_call(name: str, arguments: Mapping[str, Any], max_length: int) -> str:
    parts = [
        f"{k}={_short(v, max_length) if v is not _MASK else repr(_MASK)}"
        for k, v in arguments.items()
    ]
    return f"{name}({', '.join(parts)})"


def _is_internal(filename: str) -> bool:
    return os.path.dirname(os.path.abspath(filename)) == _PACKAGE_DIR


def _external_frame() -> FrameType | None:
    """The innermost frame of the current stack that is outside python_wrap."""
    frame: FrameType | None = sys._getframe(1)
    while frame is not None and _is_internal(frame.f_code.co_filename):
        frame = frame.f_back
    return frame


def _caller_location() -> str:
    frame = _external_frame()
    if frame is None:
        return "<unknown>"  # pragma: no cover
    return f"{frame.f_code.co_filename}:{frame.f_lineno}"


# ---------------------------------------------------------------------------- log_args


@overload
def log_args(func: Callable[P, R], /) -> Callable[P, R]: ...
@overload
def log_args(
    func: None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.DEBUG,
    redact: Iterable[str] = DEFAULT_REDACT,
    max_length: int = 200,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...
def log_args(
    func: Callable[P, R] | None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.DEBUG,
    redact: Iterable[str] = DEFAULT_REDACT,
    max_length: int = 200,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Log each call's arguments, by parameter name, before running it.

    The message looks like ``Calling f(x=1, password='***')``. Arguments are bound to the
    function's signature so positional values are named too.

    Args:
        func: The function, when used as a bare ``@log_args``.
        logger: A ``logging.Logger``, a logger name, or ``None`` for the package logger.
        level: Log level. Defaults to ``DEBUG`` (v0.1 logged at ``INFO``).
        redact: Parameter-name fragments whose values are shown as ``'***'``
            (case-insensitive substring match). Replaces the default
            :data:`DEFAULT_REDACT`; pass ``()`` to disable redaction.
        max_length: Longest rendering of a single value before it is truncated.

    Returns:
        The decorated function, with its signature preserved.

    Example:
        >>> @log_args(logger="app")
        ... def login(user, password): ...
    """
    secret = _redact_predicate(tuple(redact))

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        lg = _core.get_logger(logger)
        name = _core.qualname(f)
        sig = _signature(f)

        def start(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            if lg.isEnabledFor(level):
                bound = _bind_arguments(sig, args, kwargs, secret)
                lg.log(level, "Calling %s", _format_call(name, bound, max_length))

        return _instrument(f, start, lambda state, ok, value: None)

    return decorate if func is None else decorate(func)


# -------------------------------------------------------------------------- log_return


@overload
def log_return(func: Callable[P, R], /) -> Callable[P, R]: ...
@overload
def log_return(
    func: None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.DEBUG,
    max_length: int = 200,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...
def log_return(
    func: Callable[P, R] | None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.DEBUG,
    max_length: int = 200,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Log the value a function returns, or the exception type it raises.

    Logs ``f returned <value>``; if the function raises, logs ``f raised ValueError`` and
    re-raises the exception unchanged.

    Args:
        func: The function, when used as a bare ``@log_return``.
        logger: A ``logging.Logger``, a logger name, or ``None`` for the package logger.
        level: Log level. Defaults to ``DEBUG``.
        max_length: Longest rendering of the return value before it is truncated.

    Returns:
        The decorated function, with its signature preserved.

    Example:
        >>> @log_return
        ... def add(a, b):
        ...     return a + b
    """

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        lg = _core.get_logger(logger)
        name = _core.qualname(f)

        def finish(state: None, ok: bool, value: Any) -> None:
            if not lg.isEnabledFor(level):
                return
            if ok:
                lg.log(level, "%s returned %s", name, _short(value, max_length))
            else:
                lg.log(level, "%s raised %s", name, type(value).__name__)

        return _instrument(f, lambda args, kwargs: None, finish)

    return decorate if func is None else decorate(func)


# ------------------------------------------------------------------------------ audit


@dataclass(frozen=True)
class AuditRecord:
    """One audited call.

    Attributes:
        function: Qualified name of the audited function.
        timestamp: When the call started (timezone-aware, UTC).
        caller: ``"file:line"`` of the code that called the function.
        arguments: Parameter name to value; sensitive parameters hold ``'***'``.
        status: ``"success"`` or ``"error"``.
        error: ``repr`` of the exception, or ``None``.
        duration: Seconds the call took.
        result: The return value, only when ``include_result=True`` (else ``None``).
    """

    function: str
    timestamp: datetime
    caller: str
    arguments: dict[str, Any]
    status: str
    error: str | None
    duration: float
    result: Any = None


@overload
def audit(func: Callable[P, R], /) -> Callable[P, R]: ...
@overload
def audit(
    func: None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.INFO,
    redact: Iterable[str] = DEFAULT_REDACT,
    include_result: bool = False,
    sink: Callable[[AuditRecord], None] | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...
def audit(
    func: Callable[P, R] | None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.INFO,
    redact: Iterable[str] = DEFAULT_REDACT,
    include_result: bool = False,
    sink: Callable[[AuditRecord], None] | None = None,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Record who called a function, with what, and how it ended.

    Every call produces an :class:`AuditRecord`. One log line is emitted with the record
    attached as ``extra={"audit": record}`` (read it from ``LogRecord.audit``), and
    ``sink(record)`` is called if given. Exceptions are recorded and re-raised. An
    exception raised by ``sink`` is logged and swallowed.

    Args:
        func: The function, when used as a bare ``@audit``.
        logger: A ``logging.Logger``, a logger name, or ``None`` for the package logger.
        level: Log level of the audit line.
        redact: Parameter-name fragments whose values are masked as ``'***'``;
            ``()`` disables redaction.
        include_result: Store the return value in ``record.result``.
        sink: Callable receiving every record, e.g. to write to a database.

    Returns:
        The decorated function, with its signature preserved.

    Example:
        >>> records = []
        >>> @audit(sink=records.append)
        ... def delete_user(user_id, token): ...
    """
    secret = _redact_predicate(tuple(redact))

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        lg = _core.get_logger(logger)
        name = _core.qualname(f)
        sig = _signature(f)

        def start(
            args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> tuple[datetime, str, dict[str, Any], float]:
            return (
                datetime.now(timezone.utc),
                _caller_location(),
                _bind_arguments(sig, args, kwargs, secret),
                _core.perf_counter(),
            )

        def finish(
            state: tuple[datetime, str, dict[str, Any], float], ok: bool, value: Any
        ) -> None:
            timestamp, caller, arguments, began = state
            record = AuditRecord(
                function=name,
                timestamp=timestamp,
                caller=caller,
                arguments=arguments,
                status="success" if ok else "error",
                error=None if ok else _safe_repr(value),
                duration=_core.perf_counter() - began,
                result=value if (ok and include_result) else None,
            )
            lg.log(
                level,
                "audit %s called from %s: %s%s (%.3f s)",
                _format_call(name, arguments, 200),
                caller,
                record.status,
                f" {record.error}" if record.error else "",
                record.duration,
                extra={"audit": record},
            )
            if sink is not None:
                try:
                    sink(record)
                except Exception:
                    lg.exception("audit sink for %s failed", name)

        return _instrument(f, start, finish)

    return decorate if func is None else decorate(func)


# ------------------------------------------------------------------------------ trace


@overload
def trace(func: Callable[P, R], /) -> Callable[P, R]: ...
@overload
def trace(
    func: None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.DEBUG,
    limit: int | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...
def trace(
    func: Callable[P, R] | None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.DEBUG,
    limit: int | None = None,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Log entry, the call stack that led to the call, and exit.

    Three messages per call: ``Entering f``, the call stack (python_wrap's own frames are
    left out, outermost first), and ``Exiting f`` (or ``Exiting f (raised ValueError)``).

    Args:
        func: The function, when used as a bare ``@trace``.
        logger: A ``logging.Logger``, a logger name, or ``None`` for the package logger.
        level: Log level. Defaults to ``DEBUG``.
        limit: Keep only this many innermost frames; ``None`` keeps all.

    Returns:
        The decorated function, with its signature preserved.

    Example:
        >>> @trace(limit=5)
        ... def handler(): ...
    """

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        lg = _core.get_logger(logger)
        name = _core.qualname(f)

        def start(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            if not lg.isEnabledFor(level):
                return
            lg.log(level, "Entering %s", name)
            frame = _external_frame()
            if frame is not None:
                entries: list[traceback.FrameSummary] = list(traceback.extract_stack(frame))
                if limit is not None:
                    entries = entries[-limit:] if limit > 0 else []
                text = "".join(traceback.format_list(entries)).rstrip()
                lg.log(level, "Call stack for %s:\n%s", name, text)

        def finish(state: None, ok: bool, value: Any) -> None:
            if not lg.isEnabledFor(level):
                return
            suffix = "" if ok else f" (raised {type(value).__name__})"
            lg.log(level, "Exiting %s%s", name, suffix)

        return _instrument(f, start, finish)

    return decorate if func is None else decorate(func)
