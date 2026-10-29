"""Logging decorators: argument/return logging, audit records and call-stack tracing.

All of them log through `python_wrap._core.get_logger`, never configure logging,
and support plain functions, ``async def`` functions and methods.
"""

from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import os
import reprlib
import sys
import traceback
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import FrameType, MappingProxyType, TracebackType
from typing import Any, TypeVar, cast, overload

from python_wrap import _core, config
from python_wrap._core import F, LoggerLike, P, R
from python_wrap.redaction import DEFAULT_REDACT
from python_wrap.redaction import is_sensitive as _is_sensitive

__all__ = [
    "DEFAULT_REDACT",
    "AuditRecord",
    "audit",
    "current_log_context",
    "log_args",
    "log_context",
    "log_return",
    "trace",
]

S = TypeVar("S")

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


def _redact_predicate(
    redact: Iterable[str] | None, match: str = "segment"
) -> Callable[[str], bool]:
    """Build a "does this name look sensitive" test.

    Whole-segment matching is the default so ``author`` and ``session_date`` are not
    mistaken for secrets; ``match="substring"`` keeps the old, looser behaviour.
    """
    needles = tuple(n for n in (DEFAULT_REDACT if redact is None else redact) if n)
    if not needles:
        return lambda name: False
    return lambda name: _is_sensitive(name, needles, match=match)


def _short(value: object, max_length: int) -> str:
    """``repr`` with container truncation and a hard character limit."""
    r = reprlib.Repr()
    r.maxstring = r.maxother = max_length
    r.maxlist = r.maxtuple = r.maxset = r.maxdict = r.maxdeque = r.maxarray = 6
    text = r.repr(value)  # reprlib already survives a failing __repr__
    if max_length >= 0 and len(text) > max_length:
        text = text[: max(max_length - 3, 0)] + "..."
    return text


class _ArgBinder:
    """Maps a call's arguments to parameter names, masking sensitive ones.

    The signature is analysed once. A call that passes plain positional parameters only
    (no keywords, no ``*args`` / ``**kwargs`` in the signature, no excess arguments) is
    named straight from a precomputed name tuple; everything else goes through
    ``Signature.bind``. Redaction is decided once per parameter name.
    """

    def __init__(self, sig: inspect.Signature | None, is_secret: Callable[[str], bool]) -> None:
        self.sig = sig
        self.is_secret = is_secret
        self.names: tuple[str, ...] = ()
        self.flags: tuple[bool, ...] = ()
        self.secret_names: dict[str, bool] = {}
        self.fast_max = -1
        if sig is not None:
            params = list(sig.parameters.values())
            self.secret_names = {p.name: is_secret(p.name) for p in params}
            plain = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            if all(p.kind in plain for p in params):
                self.names = tuple(p.name for p in params)
                self.flags = tuple(self.secret_names[n] for n in self.names)
                self.fast_max = len(params)

    def _secret(self, name: str) -> bool:
        known = self.secret_names.get(name)
        return self.is_secret(name) if known is None else known

    def __call__(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        if not kwargs and len(args) <= self.fast_max:
            return {
                n: (_MASK if secret else v)
                for n, secret, v in zip(self.names, self.flags, args, strict=False)
            }
        sig = self.sig
        bound = None
        if sig is not None:
            try:
                bound = sig.bind(*args, **kwargs)
            except TypeError:
                bound = None
        named: dict[str, Any] = {}
        if sig is None or bound is None:  # the call is invalid anyway; show what we were given
            named = {f"arg{i}": v for i, v in enumerate(args)}
            named.update(kwargs)
        else:
            for name, value in bound.arguments.items():
                if sig.parameters[name].kind is inspect.Parameter.VAR_KEYWORD:
                    named.update(value)
                else:
                    named[name] = value
        return {k: (_MASK if self._secret(k) else v) for k, v in named.items()}


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


_internal_files: dict[str, bool] = {}


def _is_internal(filename: str) -> bool:
    try:
        return _internal_files[filename]
    except KeyError:
        result = os.path.dirname(os.path.abspath(filename)) == _PACKAGE_DIR
        if len(_internal_files) < 4096:  # co_filename values are few; stay bounded anyway
            _internal_files[filename] = result
        return result


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


# ----------------------------------------------------------------------- log context

_log_fields: contextvars.ContextVar[Mapping[str, Any]] = contextvars.ContextVar(
    "python_wrap_log_fields", default=MappingProxyType({})
)


def current_log_context() -> dict[str, Any]:
    """Return a copy of the fields bound by the enclosing `log_context` blocks.

    Returns:
        A new dict (empty when no `log_context` is active); changing it has no effect.

    Example:
        >>> with log_context(request_id="r1"):
        ...     current_log_context()
        {'request_id': 'r1'}
    """
    return dict(_log_fields.get())


def _safe_str(value: object) -> str:
    try:
        return str(value)
    except BaseException:
        return f"<unprintable {type(value).__name__}>"


def _log(
    lg: logging.Logger,
    level: int,
    msg: str,
    *args: Any,
    context: Mapping[str, Any] | None = None,
    suffix: bool = True,
    extra: dict[str, Any] | None = None,
) -> None:
    """``lg.log`` plus the bound context: ``extra["context"]`` and a ``[k=v]`` suffix."""
    fields = _log_fields.get() if context is None else context
    if fields:
        extra = {**(extra or {}), "context": dict(fields)}
        if suffix:
            text = " ".join(f"{k}={_safe_str(v)}" for k, v in fields.items())
            msg = f"{msg} [{text.replace('%', '%%')}]"
    lg.log(level, msg, *args, extra=extra)


class _LogContext:
    """Context manager (sync and async) and decorator returned by `log_context`."""

    def __init__(self, fields: dict[str, Any]) -> None:
        self._fields = fields
        self._tokens: list[contextvars.Token[Mapping[str, Any]]] = []

    def __enter__(self) -> dict[str, Any]:
        merged = {**_log_fields.get(), **self._fields}
        self._tokens.append(_log_fields.set(merged))
        return dict(merged)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _log_fields.reset(self._tokens.pop())

    async def __aenter__(self) -> dict[str, Any]:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc, tb)

    def __call__(self, func: F) -> F:
        fields = self._fields
        if _core.is_async(func):

            @functools.wraps(func)
            async def awrapper(*args: Any, **kwargs: Any) -> Any:
                async with _LogContext(fields):
                    return await func(*args, **kwargs)

            return cast("F", awrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with _LogContext(fields):
                return func(*args, **kwargs)

        return cast("F", wrapper)


def log_context(**fields: Any) -> _LogContext:
    """Bind extra fields to every python-wrap log line made inside a block or call.

    Works as a context manager (``with`` and ``async with``) and as a decorator for plain
    and ``async def`` functions. Blocks nest: inner fields add to, and override, outer ones.
    The fields live in a `contextvars.ContextVar`, so each thread and asyncio task sees its
    own; a new thread starts empty unless you run it in a copied context
    (``contextvars.copy_context().run(...)``; `run_in_thread` does this for you), or
    unless ``sys.flags.thread_inherit_context`` is set (the default on free-threaded
    builds).

    `log_args`, `log_return`, `audit`, `trace` and `timing` append `` [key=value ...]`` to
    their message and attach the dict as ``LogRecord.context``; `AuditRecord.context`
    holds it too.

    Args:
        **fields: The fields to bind, e.g. ``request_id="abc"``.

    Returns:
        An object usable as ``with log_context(...) as fields``, ``async with`` or ``@``.
        The ``as`` target is the merged dict of fields.

    Example:
        >>> @log_return(logger="app")
        ... def handle(): ...
        >>> with log_context(request_id="r1"):
        ...     handle()
    """
    return _LogContext(dict(fields))


# ---------------------------------------------------------------------------- log_args


@overload
def log_args(func: F, /) -> F: ...
@overload
def log_args(
    func: None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.DEBUG,
    redact: Iterable[str] | None = None,
    redact_extra: Iterable[str] = (),
    redact_match: str = "segment",
    max_length: int = 200,
) -> Callable[[F], F]: ...
def log_args(
    func: Callable[..., Any] | None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.DEBUG,
    redact: Iterable[str] | None = None,
    redact_extra: Iterable[str] = (),
    redact_match: str = "segment",
    max_length: int = 200,
) -> Any:
    """Log each call's arguments, by parameter name, before running it.

    The message looks like ``Calling f(x=1, password='***')``. Arguments are bound to the
    function's signature so positional values are named too.

    Args:
        func: The function, when used as a bare ``@log_args``.
        logger: A ``logging.Logger``, a logger name, or ``None`` for the package logger.
        level: Log level. Defaults to ``DEBUG`` (v0.1 logged at ``INFO``).
        redact: Parameter-name fragments whose values are shown as ``'***'``.
            ``None`` (default) uses `DEFAULT_REDACT` or the list set by
            `python_wrap.config.configure`. A list given here **replaces** that; pass
            ``()`` to disable redaction.
        redact_extra: Fragments to mask **in addition** to the list above (never
            replaces it). Added to any `redact_extra` set via `configure`.
        redact_match: ``"segment"`` (default) masks a name only when a whole word
            segment matches, so ``author`` and ``session_date`` stay visible while
            ``auth_token`` and ``session_id`` do not. ``"substring"`` restores the old
            looser matching.
        max_length: Longest rendering of a single value before it is truncated.

    Returns:
        The decorated function, with its signature preserved.

    Example:
        >>> @log_args(logger="app")
        ... def login(user, password): ...
    """
    secret = _redact_predicate(
        config.redaction(redact, DEFAULT_REDACT) + tuple(redact_extra), redact_match
    )

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        lg = _core.get_logger(logger)
        name = _core.qualname(f)
        binder = _ArgBinder(_signature(f), secret)

        def start(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            if lg.isEnabledFor(level):
                bound = binder(args, kwargs)
                _log(lg, level, "Calling %s", _format_call(name, bound, max_length))

        return _instrument(f, start, lambda state, ok, value: None)

    return decorate if func is None else decorate(func)


# -------------------------------------------------------------------------- log_return


@overload
def log_return(func: F, /) -> F: ...
@overload
def log_return(
    func: None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.DEBUG,
    max_length: int = 200,
) -> Callable[[F], F]: ...
def log_return(
    func: Callable[..., Any] | None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.DEBUG,
    max_length: int = 200,
) -> Any:
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
                _log(lg, level, "%s returned %s", name, _short(value, max_length))
            else:
                _log(lg, level, "%s raised %s", name, type(value).__name__)

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
        context: Fields bound by `log_context` when the call started (empty if none).
    """

    function: str
    timestamp: datetime
    caller: str
    arguments: dict[str, Any]
    status: str
    error: str | None
    duration: float
    result: Any = None
    context: dict[str, Any] = field(default_factory=dict)


@overload
def audit(func: F, /) -> F: ...
@overload
def audit(
    func: None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.INFO,
    redact: Iterable[str] | None = None,
    redact_extra: Iterable[str] = (),
    redact_match: str = "segment",
    include_result: bool = False,
    sink: Callable[[AuditRecord], None] | None = None,
) -> Callable[[F], F]: ...
def audit(
    func: Callable[..., Any] | None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.INFO,
    redact: Iterable[str] | None = None,
    redact_extra: Iterable[str] = (),
    redact_match: str = "segment",
    include_result: bool = False,
    sink: Callable[[AuditRecord], None] | None = None,
) -> Any:
    """Record who called a function, with what, and how it ended.

    Every call produces an `AuditRecord`. One log line is emitted with the record
    attached as ``extra={"audit": record}`` (read it from ``LogRecord.audit``), and
    ``sink(record)`` is called if given. Exceptions are recorded and re-raised. An
    exception raised by ``sink`` is logged and swallowed.

    Args:
        func: The function, when used as a bare ``@audit``.
        logger: A ``logging.Logger``, a logger name, or ``None`` for the package logger.
        level: Log level of the audit line.
        redact: Parameter-name fragments whose values are masked as ``'***'``;
            ``None`` (default) uses `DEFAULT_REDACT` or the list set by
            `python_wrap.config.configure`; a list given here replaces it and ``()``
            disables redaction.
        redact_extra: Fragments to mask in addition to the list above (never
            replaces it). Added to any `redact_extra` set via `configure`.
        redact_match: ``"segment"`` (default) or ``"substring"``; see `log_args`.
        include_result: Store the return value in ``record.result``.
        sink: Callable receiving every record, e.g. to write to a database.

    Returns:
        The decorated function, with its signature preserved.

    Example:
        >>> records = []
        >>> @audit(sink=records.append)
        ... def delete_user(user_id, token): ...
    """
    secret = _redact_predicate(
        config.redaction(redact, DEFAULT_REDACT) + tuple(redact_extra), redact_match
    )

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        lg = _core.get_logger(logger)
        name = _core.qualname(f)
        binder = _ArgBinder(_signature(f), secret)

        def start(
            args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> tuple[datetime, str, dict[str, Any], float, dict[str, Any]] | None:
            if sink is None and not lg.isEnabledFor(level):
                return None  # nobody would see the record: skip building it
            return (
                datetime.now(timezone.utc),
                _caller_location(),
                binder(args, kwargs),
                _core.perf_counter(),
                current_log_context(),
            )

        def finish(
            state: tuple[datetime, str, dict[str, Any], float, dict[str, Any]] | None,
            ok: bool,
            value: Any,
        ) -> None:
            if state is None:
                return
            timestamp, caller, arguments, began, context = state
            record = AuditRecord(
                function=name,
                timestamp=timestamp,
                caller=caller,
                arguments=arguments,
                status="success" if ok else "error",
                error=None if ok else _safe_repr(value),
                duration=_core.perf_counter() - began,
                result=value if (ok and include_result) else None,
                context=context,
            )
            _log(
                lg,
                level,
                "audit %s called from %s: %s%s (%.3f s)",
                _format_call(name, arguments, 200),
                caller,
                record.status,
                f" {record.error}" if record.error else "",
                record.duration,
                context=context,
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


def _otel_hooks(
    f: Callable[..., Any], span_name: str | None
) -> tuple[Callable[[], Any], Callable[[Any, bool, Any], None]]:
    """Import OpenTelemetry now and return ``(open_span, close_span)`` hooks for ``trace``."""
    try:
        from opentelemetry import context as otel_context
        from opentelemetry import trace as otel_trace
    except ImportError as exc:
        raise ImportError(
            "trace(otel=True) needs the 'opentelemetry-api' package: pip install opentelemetry-api"
        ) from exc
    tracer = otel_trace.get_tracer("python_wrap")
    name = span_name or _core.qualname(f)
    module = getattr(f, "__module__", None)
    qual = getattr(f, "__qualname__", name)

    def open_span() -> Any:
        attributes: dict[str, Any] = {"code.function": qual.rpartition(".")[2]}
        if module:
            attributes["code.namespace"] = module
        for key, value in current_log_context().items():
            attributes[f"python_wrap.context.{key}"] = (
                value if isinstance(value, (str, bool, int, float)) else str(value)
            )
        span = tracer.start_span(name, attributes=attributes)
        token = otel_context.attach(otel_trace.set_span_in_context(span))
        return span, token

    def close_span(state: Any, ok: bool, value: Any) -> None:
        span, token = state
        try:
            if not ok:
                if isinstance(value, Exception):
                    span.record_exception(value)
                span.set_status(
                    otel_trace.Status(otel_trace.StatusCode.ERROR, type(value).__name__)
                )
        finally:
            otel_context.detach(token)
            span.end()

    return open_span, close_span


@overload
def trace(func: F, /) -> F: ...
@overload
def trace(
    func: None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.DEBUG,
    limit: int | None = None,
    otel: bool = False,
    span_name: str | None = None,
) -> Callable[[F], F]: ...
def trace(
    func: Callable[..., Any] | None = None,
    /,
    *,
    logger: LoggerLike = None,
    level: int = logging.DEBUG,
    limit: int | None = None,
    otel: bool = False,
    span_name: str | None = None,
) -> Any:
    """Log entry, the call stack that led to the call, and exit.

    Three messages per call: ``Entering f``, the call stack (python_wrap's own frames are
    left out, outermost first), and ``Exiting f`` (or ``Exiting f (raised ValueError)``).

    With ``otel=True`` the call is also wrapped in an OpenTelemetry span (see Args).

    Args:
        func: The function, when used as a bare ``@trace``.
        logger: A ``logging.Logger``, a logger name, or ``None`` for the package logger.
        level: Log level. Defaults to ``DEBUG``.
        limit: Keep only this many innermost frames; ``None`` keeps all.
        otel: Open an OpenTelemetry span around every call, using the tracer
            ``opentelemetry.trace.get_tracer("python_wrap")``. The span is current for the
            whole call (also across ``await``), carries ``code.function``,
            ``code.namespace`` and the fields of the active `log_context` as
            ``python_wrap.context.<name>`` attributes, and on an exception records it and
            sets the error status. ``opentelemetry-api`` is imported only when this is
            true; python-wrap itself never depends on it.
        span_name: Span name; defaults to the function's qualified name.

    Returns:
        The decorated function, with its signature preserved.

    Raises:
        ImportError: At decoration time, if ``otel=True`` and ``opentelemetry-api`` is not
            installed.

    Example:
        >>> @trace(limit=5)
        ... def handler(): ...
    """

    def decorate(f: Callable[P, R]) -> Callable[P, R]:
        lg = _core.get_logger(logger)
        name = _core.qualname(f)
        open_span, close_span = _otel_hooks(f, span_name) if otel else (None, None)

        def start(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
            span_state = open_span() if open_span is not None else None
            if not lg.isEnabledFor(level):
                return span_state
            _log(lg, level, "Entering %s", name)
            frame = _external_frame()
            if frame is not None:
                entries: list[traceback.FrameSummary] = list(traceback.extract_stack(frame))
                if limit is not None:
                    entries = entries[-limit:] if limit > 0 else []
                text = "".join(traceback.format_list(entries)).rstrip()
                _log(lg, level, "Call stack for %s:\n%s", name, text, suffix=False)
            return span_state

        def finish(state: Any, ok: bool, value: Any) -> None:
            try:
                if lg.isEnabledFor(level):
                    suffix = "" if ok else f" (raised {type(value).__name__})"
                    _log(lg, level, "Exiting %s%s", name, suffix)
            finally:
                if close_span is not None:
                    close_span(state, ok, value)

        return _instrument(f, start, finish)

    return decorate if func is None else decorate(func)
