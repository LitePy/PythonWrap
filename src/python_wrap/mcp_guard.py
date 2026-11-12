"""Timeouts, retries and per-server circuit breakers around MCP tool calls.

An agent that calls tools on Model Context Protocol servers inherits their failure modes:
a server that hangs holds the whole agent turn, a crashed server is called again on every
turn, and a blind retry repeats a tool that already wrote something. `mcp_guard` wraps the
call path without importing any MCP SDK:

* a ``call_tool(name, arguments)`` function (sync or ``async def``), or
* any object with a ``call_tool`` method (an MCP ``ClientSession``, an agent framework's
  MCP server object): you get a proxy whose ``call_tool`` is guarded and whose other
  attributes are the original's.

Each call is admitted by the server's `CircuitBreaker` (one per server name, shared by
every guard for that server), retried only for tools you mark safe to repeat, limited by a
per-attempt timeout, and stops cooperatively: the current `CancelToken` (an enclosing
`timeout`, a `RunGuard` that ran out of time or was cancelled) is checked before every
attempt, and an in-flight ``async`` call is cancelled as soon as the token fires.

`python_wrap.doctor` reports a guard without a timeout (PW020) and a guard that retries
every tool, side effects included (PW021).
"""

from __future__ import annotations

import asyncio
import fnmatch
import functools
import threading
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, cast

from python_wrap import _core
from python_wrap._runtime import seconds as _parse_seconds
from python_wrap.circuit import CircuitBreaker
from python_wrap.concurrency import _current_token, checkpoint

__all__ = ["MCPGuard", "mcp_breaker", "mcp_guard"]

_LAYER_ATTR = "__python_wrap_layer__"
_BREAKERS: dict[str, CircuitBreaker] = {}
_BREAKERS_LOCK = threading.Lock()
_DEFAULT_RETRY_ON: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, OSError)


def mcp_breaker(server: str, **options: Any) -> CircuitBreaker:
    """The shared `CircuitBreaker` of MCP server ``server`` (created on first use).

    Every `mcp_guard` for the same server name uses this breaker, so one dead server is
    detected once for the whole process. ``options`` (`CircuitBreaker` arguments) apply
    only when the breaker is created.

    Example:
        >>> mcp_breaker("docs") is mcp_breaker("docs")
        True
    """
    if not server:
        raise ValueError("server must be a non-empty name")
    with _BREAKERS_LOCK:
        found = _BREAKERS.get(server)
        if found is None:
            settings: dict[str, Any] = {"failure_threshold": 5, "recovery_timeout": 30.0}
            settings.update(options)
            settings.setdefault("name", f"mcp:{server}")
            found = _BREAKERS[server] = CircuitBreaker(**settings)
        return found


def _server_name(target: Any, given: str | None) -> str:
    if given:
        return given
    for attr in ("name", "server_name"):
        value = getattr(target, attr, None)
        if isinstance(value, str) and value:
            return value
    owner = getattr(target, "__self__", None)
    if owner is not None:
        return _server_name(owner, None)
    return getattr(target, "__qualname__", type(target).__qualname__)


def _globs(value: Iterable[str] | str | Callable[[str], bool] | None) -> Any:
    if value is None or callable(value):
        return value
    patterns = (value,) if isinstance(value, str) else tuple(value)
    return lambda name: any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _retry_decorator(retry: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]] | None:
    if retry is None or retry is False:
        return None
    if isinstance(retry, bool) or (isinstance(retry, int) and retry < 1):
        raise ValueError("retry must be a positive number of attempts or a retry decorator")
    if isinstance(retry, int):
        from python_wrap.retry import retry as make_retry

        return cast(
            Callable[[Callable[..., Any]], Callable[..., Any]],
            make_retry(max_attempts=retry, delay=0.2, backoff=2.0, on=_DEFAULT_RETRY_ON),
        )
    if callable(retry):
        return cast(Callable[[Callable[..., Any]], Callable[..., Any]], retry)
    raise TypeError("retry must be None, a number of attempts or a retry decorator")


def _tool_name(args: Sequence[Any], kwargs: Mapping[str, Any]) -> str:
    name = kwargs.get("name", kwargs.get("tool_name", args[0] if args else None))
    return name if isinstance(name, str) else "?"


async def _cancellable(awaitable: Awaitable[Any], label: str) -> Any:
    """Await ``awaitable``; cancel it as soon as the current `CancelToken` fires."""
    token = _current_token.get()
    if token is None:
        return await awaitable
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=0.02)
            if done:
                return task.result()
            if token.cancelled:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                _core.emit("mcp.cancelled", label)
                token.raise_if_cancelled()
    except BaseException:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise


class MCPGuard:
    """A guarded MCP client: ``call_tool`` goes through breaker, retry and timeout.

    Built by `mcp_guard` from an object with a ``call_tool`` method; every other
    attribute (``list_tools``, ``initialize``...) is forwarded unchanged.

    Attributes:
        server: The server name (its breaker is ``mcp_breaker(server)``).
        breaker: The breaker in use, or None.
        timeout: Per-attempt timeout in seconds, or None.
        wrapped: The original object.
    """

    def __init__(self, target: Any, call: Callable[..., Any], settings: dict[str, Any]) -> None:
        self.wrapped = target
        self._call = call
        self.server: str = settings["server"]
        self.timeout: float | None = settings["timeout"]
        self.breaker: CircuitBreaker | None = settings.pop("_breaker")
        setattr(self, _LAYER_ATTR, ("mcp_guard", settings))

    @property
    def call_tool(self) -> Callable[..., Any]:
        """The guarded ``call_tool`` (same signature and sync/async kind as the original)."""
        return self._call

    def __getattr__(self, name: str) -> Any:
        return getattr(self.wrapped, name)

    def __repr__(self) -> str:
        return f"MCPGuard(server={self.server!r}, timeout={self.timeout!r})"


def mcp_guard(
    target: Any,
    *,
    timeout: float | str | None = 30.0,
    retry: int | Callable[[Callable[..., Any]], Callable[..., Any]] | None = None,
    retry_tools: Iterable[str] | str | Callable[[str], bool] | None = None,
    breaker: bool | CircuitBreaker | Mapping[str, Any] | None = True,
    server: str | None = None,
) -> Any:
    """Guard an MCP tool-call path with a timeout, retries and a per-server breaker.

    Args:
        target: A ``call_tool(name, arguments=None, ...)`` function (sync or async), or
            an object with such a ``call_tool`` method (then an `MCPGuard` proxy is
            returned).
        timeout: Per-attempt limit in seconds (or ``"30s"``); None disables it, which
            `doctor` reports as PW020.
        retry: Attempts per call (an int, retrying ``ConnectionError``, ``TimeoutError``
            and ``OSError`` with backoff), or a decorator such as
            ``retry(max_attempts=3, on=...)``. None never retries.
        retry_tools: Which tools may be retried: glob patterns or a predicate on the tool
            name. MCP tools can have side effects, so mark only those safe to repeat;
            None retries every tool (PW021).
        breaker: True uses the shared per-server breaker (`mcp_breaker`); a mapping
            creates it with those options on first use; a `CircuitBreaker` is used as
            is; False or None disables it. A tool result with ``isError`` set is a tool
            answer, not a server failure: it is returned and counts as a success.
        server: The server name for the breaker; read from ``target.name`` (or the
            function name) by default.

    Returns:
        The guarded function, or an `MCPGuard` for an object.

    Example:
        ```python
        from python_wrap import mcp_guard

        calls = []

        def call_tool(name: str, arguments: dict | None = None) -> dict:
            calls.append(name)
            if len(calls) == 1:
                raise ConnectionError("server restarting")
            return {"content": [{"type": "text", "text": "42"}], "isError": False}

        guarded = mcp_guard(
            call_tool, server="calc", timeout=5, retry=2, retry_tools=["get_*"], breaker=False
        )
        assert guarded("get_answer", {})["content"][0]["text"] == "42"
        assert calls == ["get_answer", "get_answer"]
        ```
    """
    limit = None if timeout is None else _parse_seconds(timeout)
    if limit is not None and limit <= 0:
        raise ValueError("timeout must be positive or None")
    decorate = _retry_decorator(retry)
    may_retry = _globs(retry_tools)
    method = getattr(target, "call_tool", None)
    is_object = callable(method)
    if not is_object and not callable(target):
        raise TypeError("mcp_guard expects a call_tool function or an object with call_tool")
    call = cast(Callable[..., Any], method if is_object else target)
    name = _server_name(target, server)
    chosen: CircuitBreaker | None
    if breaker is True:
        chosen = mcp_breaker(name)
    elif isinstance(breaker, CircuitBreaker):
        chosen = breaker
    elif isinstance(breaker, Mapping):
        chosen = mcp_breaker(name, **dict(breaker))
    elif breaker is None or breaker is False:
        chosen = None
    else:
        raise TypeError("breaker must be True, False, None, a CircuitBreaker or a mapping")
    settings: dict[str, Any] = {
        "server": name,
        "timeout": limit,
        "retried": decorate is not None,
        "retry_tools": retry_tools is not None,
        "breaker": chosen is not None,
        "_breaker": chosen,
    }
    guarded = _build(call, name, limit, decorate, may_retry, chosen)
    if is_object:
        return MCPGuard(target, guarded, settings)
    settings.pop("_breaker")
    setattr(guarded, _LAYER_ATTR, ("mcp_guard", settings))
    return guarded


def _build(
    call: Callable[..., Any],
    server: str,
    limit: float | None,
    decorate: Callable[[Callable[..., Any]], Callable[..., Any]] | None,
    may_retry: Callable[[str], bool] | None,
    breaker: CircuitBreaker | None,
) -> Callable[..., Any]:
    from python_wrap.concurrency import timeout as make_timeout

    is_async = _core.is_async(call)
    label = f"mcp:{server}"

    if is_async:

        async def attempt_async(*args: Any, **kwargs: Any) -> Any:
            checkpoint()
            return await _cancellable(call(*args, **kwargs), label)

        if limit is not None:
            attempt_async = make_timeout(limit)(attempt_async)
        retried_async = decorate(attempt_async) if decorate is not None else attempt_async

        async def through_async(*args: Any, **kwargs: Any) -> Any:
            tool = _tool_name(args, kwargs)
            runner = (
                retried_async
                if decorate is not None and (may_retry is None or may_retry(tool))
                else attempt_async
            )
            if breaker is None:
                return await runner(*args, **kwargs)
            return await breaker.call(runner, *args, **kwargs)

        @functools.wraps(call)
        async def guarded_async(*args: Any, **kwargs: Any) -> Any:
            checkpoint()
            return await through_async(*args, **kwargs)

        return guarded_async

    def attempt(*args: Any, **kwargs: Any) -> Any:
        checkpoint()
        return call(*args, **kwargs)

    if limit is not None:
        attempt = make_timeout(limit)(attempt)
    retried = decorate(attempt) if decorate is not None else attempt

    @functools.wraps(call)
    def guarded(*args: Any, **kwargs: Any) -> Any:
        checkpoint()
        tool = _tool_name(args, kwargs)
        runner = (
            retried if decorate is not None and (may_retry is None or may_retry(tool)) else attempt
        )
        if breaker is None:
            return runner(*args, **kwargs)
        return breaker.call(runner, *args, **kwargs)

    return guarded


# ----------------------------------------------------------------------------- doctor


def doctor_findings(layers: Sequence[Any]) -> list[Any]:
    """PW020 (MCP/framework tool call without a timeout) and PW021 (blanket MCP retry)."""
    from python_wrap.doctor import Finding

    found: list[Finding] = []
    for layer in layers:
        settings = layer.settings
        if layer.role in ("mcp_guard", "framework_tool") and settings.get("timeout") is None:
            what = (
                f"MCP server {settings.get('server')!r}"
                if layer.role == "mcp_guard"
                else f"{settings.get('framework')} tool {settings.get('name')!r}"
            )
            found.append(
                Finding(
                    "PW020",
                    "warning",
                    f"{what} is called without a timeout: a hung server or tool holds the "
                    "agent turn (and its run budget) forever. Pass timeout= (seconds).",
                )
            )
        if (
            layer.role == "mcp_guard"
            and settings.get("retried")
            and not settings.get("retry_tools")
        ):
            found.append(
                Finding(
                    "PW021",
                    "warning",
                    f"mcp_guard for {settings.get('server')!r} retries every tool: MCP tools "
                    "can have side effects, so a retried write may run twice. List the "
                    "tools that are safe to repeat with retry_tools=[...].",
                )
            )
    return found
