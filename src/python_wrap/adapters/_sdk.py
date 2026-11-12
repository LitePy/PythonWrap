"""Shared machinery for the provider SDK adapters (`guard_openai`, `guard_anthropic`).

Nothing here imports a provider SDK. Clients, responses, stream events and errors are
read by shape, so the same code works with the real ``openai`` / ``anthropic`` packages,
with their ``with_raw_response`` wrappers, and with hand-written fakes.

- `no_sdk_retries` / `sdk_max_retries`: turn the SDK's own retry loop off so
  python-wrap's is the only one (attempts never multiply across layers).
- `seed_limiter`: feed ``x-ratelimit-remaining-tokens``,
  ``anthropic-ratelimit-tokens-remaining`` and ``retry-after`` back into a
  `TokenRateLimiter`.
- `resume_safe` / `ResumableStream` / `StreamInterrupted`: reopen a stream that fails
  before any output, never replay one that already produced text or a tool call.
- `CacheGuard`: watch prompt-cache hits and flag prefixes that change per request.
- `GuardedClient`: the proxy both adapters build on.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import hashlib
import inspect
import json
import math
import re
import threading
import warnings
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Callable,
    Generator,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from typing import TYPE_CHECKING, Any, Generic, TypeVar, cast

from python_wrap import _core
from python_wrap.errors import PythonWrapError
from python_wrap.llm import (
    Budget,
    TokenRateLimiter,
    Usage,
    _field,
    _int,
    _maybe_await,
    _sdk_retries,
    llm_retry,
    llm_retry_after,
    llm_transient,
    read_usage,
    stream_timeout,
)

if TYPE_CHECKING:
    from python_wrap.doctor import Finding, Layer, Report

__all__ = [
    "CacheGuard",
    "CacheStats",
    "GuardedClient",
    "PromptCacheWarning",
    "ResumableStream",
    "StreamInterrupted",
    "StreamedToolCall",
    "no_sdk_retries",
    "resume_safe",
    "sdk_max_retries",
    "seed_limiter",
    "stream_transient",
]

T = TypeVar("T")
C = TypeVar("C")

_LAYER_ATTR = "__python_wrap_layer__"


# ------------------------------------------------------------------- SDK retries


def no_sdk_retries(client: C) -> C:
    """A copy of an SDK client with the SDK's own retries turned off.

    Both the OpenAI and the Anthropic Python SDKs retry 408/409/429/5xx twice by
    default. Under a python-wrap retry that multiplies: ``llm_retry(max_attempts=4)``
    around a client with ``max_retries=2`` makes up to 4 x 3 = 12 HTTP requests per
    call, and the SDK's waits ignore your `RetryBudget`. This returns
    ``client.with_options(max_retries=0)`` (or ``client.copy(max_retries=0)``): the
    original client is unchanged and shares its connection pool with the copy.

    Args:
        client: An ``OpenAI`` / ``AsyncOpenAI`` / ``Anthropic`` / ``AsyncAnthropic``
            client, or anything with ``with_options(max_retries=...)``.

    Returns:
        The client copy.

    Raises:
        TypeError: If the client has neither ``with_options`` nor ``copy``.

    Example:
        ```python
        from python_wrap.adapters import no_sdk_retries, sdk_max_retries


        class Client:  # the shape of an SDK client
            def __init__(self, max_retries: int = 2) -> None:
                self.max_retries = max_retries

            def with_options(self, *, max_retries: int) -> "Client":
                return Client(max_retries)


        client = Client()
        assert sdk_max_retries(no_sdk_retries(client)) == 0
        assert sdk_max_retries(client) == 2  # the original keeps its setting
        ```
    """
    for method in ("with_options", "copy"):
        build = getattr(client, method, None)
        if callable(build):
            return cast(C, build(max_retries=0))
    raise TypeError(
        f"{type(client).__name__} has no with_options()/copy(): build the SDK client "
        "with max_retries=0 yourself"
    )


def sdk_max_retries(client: Any) -> int | None:
    """The ``max_retries`` an SDK client (or a guard around one) is set to, or None."""
    return _sdk_retries(client)


# ------------------------------------------------------------------ rate headers

_REMAINING_HEADERS = (
    "x-ratelimit-remaining-tokens",
    "anthropic-ratelimit-tokens-remaining",
    "anthropic-ratelimit-input-tokens-remaining",
)


def _headers_of(source: Any) -> Any:
    """Response headers of a raw response, an SDK error or its ``.response``."""
    if source is None:
        return None
    headers = getattr(source, "headers", None)
    if headers is None:
        headers = getattr(getattr(source, "response", None), "headers", None)
    return headers


def _header_items(headers: Any) -> list[tuple[str, Any]]:
    if headers is None:
        return []
    try:
        items = headers.items() if hasattr(headers, "items") else headers
        return [(str(key).casefold(), value) for key, value in items]
    except (TypeError, ValueError, AttributeError):
        return []


def remaining_tokens(headers: Any) -> int | None:
    """The smallest token allowance left that the provider's rate-limit headers report."""
    found: list[int] = []
    for key, value in _header_items(headers):
        if key in _REMAINING_HEADERS:
            try:
                number = float(str(value).strip())
            except ValueError:
                continue
            if math.isfinite(number):
                found.append(max(int(number), 0))
    return min(found) if found else None


def seed_limiter(
    limiter: TokenRateLimiter, headers: Any = None, *, retry_after: float | None = None
) -> None:
    """Tell a `TokenRateLimiter` what the provider says about its quota.

    The provider knows about every client sharing your key; the local bucket only about
    this process. After each response the bucket is lowered (never raised) to the
    ``x-ratelimit-remaining-tokens`` (OpenAI) or ``anthropic-ratelimit-tokens-remaining``
    / ``anthropic-ratelimit-input-tokens-remaining`` (Anthropic) value. After a 429 with
    ``retry_after`` seconds the bucket is drained so the next reservation waits at least
    that long.

    Args:
        limiter: The limiter to adjust.
        headers: Response headers (any mapping or ``(key, value)`` pairs), or None.
        retry_after: Seconds the provider asked to wait, or None.
    """
    remaining = remaining_tokens(headers)
    wait = retry_after if retry_after is not None and math.isfinite(retry_after) else None
    if remaining is None and not wait:
        return
    with limiter._lock:
        limiter._refill()
        if remaining is not None:
            limiter._level = min(limiter._level, float(remaining))
        if wait is not None and wait > 0:
            limiter._level = min(limiter._level, -wait * limiter._rate)


# ------------------------------------------------------------------- stream events


@dataclasses.dataclass(frozen=True)
class StreamedToolCall:
    """A tool call assembled from stream events.

    Attributes:
        id: The provider's call id (``call_...`` / ``toolu_...``), when sent.
        name: The tool name, when sent.
        arguments: The raw JSON argument string as streamed (possibly incomplete for a
            pending call).
        index: Its position in the response.
    """

    id: str | None
    name: str | None
    arguments: str
    index: int

    @property
    def input(self) -> dict[str, Any]:
        """Parsed arguments, or ``{}`` when they are not (yet) a JSON object."""
        try:
            parsed = json.loads(self.arguments) if self.arguments else {}
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


class StreamInterrupted(PythonWrapError):
    """A stream failed after it had produced output; it is not replayed.

    Reopening the request would send the caller the same text twice, or ask the model
    for a tool call that may already be executing. The caller decides: show the partial
    text, run the completed tool calls, or start a new turn.

    Not a transient error: `llm_transient` returns False for it, so an outer
    `llm_retry` does not replay the request either. The original error is
    ``__cause__``.

    Attributes:
        text: The text streamed before the failure.
        tool_calls: Tool calls whose arguments were complete before the failure.
        pending: Tool calls that had started but not finished (never safe to run).
        attempts: How many times the stream was opened.
        usage: Token usage reported before the failure, if any.
    """

    def __init__(
        self,
        message: str,
        *,
        text: str,
        tool_calls: tuple[StreamedToolCall, ...],
        pending: tuple[StreamedToolCall, ...],
        attempts: int,
        usage: Usage | None,
    ) -> None:
        super().__init__(message)
        self.text = text
        self.tool_calls = tool_calls
        self.pending = pending
        self.attempts = attempts
        self.usage = usage


# Event types that carry no output: a failure after them is safe to reopen.
_PRELUDE_TYPES = frozenset(
    {
        "message_start",
        "ping",
        "response.created",
        "response.in_progress",
        "response.queued",
    }
)
# Helper events the SDKs synthesise next to the raw delta they repeat.
_DERIVED_TYPES = frozenset({"text", "input_json", "citation", "thinking", "signature"})
_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _usage_fields(raw: Any) -> dict[str, int]:
    """The token counts of a usage object or mapping, including cached-token details."""
    found: dict[str, int] = {}
    if raw is None:
        return found
    for key in _USAGE_KEYS:
        value = _int(_field(raw, key))
        if value is not None:
            found[key] = max(value, 0)
    for details in ("prompt_tokens_details", "input_tokens_details"):
        cached = _int(_field(_field(raw, details), "cached_tokens"))
        if cached is not None:
            found["cached_tokens"] = max(cached, 0)
    cached = _int(_field(raw, "cached_tokens"))
    if cached is not None:
        found["cached_tokens"] = max(cached, 0)
    return found


@dataclasses.dataclass
class _Partial:
    id: str | None
    name: str | None
    arguments: str
    index: int
    fallback: str = ""

    def done(self) -> StreamedToolCall:
        return StreamedToolCall(self.id, self.name, self.arguments or self.fallback, self.index)


class _Tracker:
    """Reads stream events of every supported shape; one per opened stream."""

    def __init__(self) -> None:
        self.committed = False
        self.text_parts: list[str] = []
        self.open: dict[Any, _Partial] = {}
        self.completed: list[StreamedToolCall] = []
        self.usage: dict[str, int] = {}
        self.model: str | None = None

    # -- helpers ---------------------------------------------------------------
    def _start(self, key: Any, call_id: Any, name: Any, arguments: Any, index: int) -> None:
        self.open[key] = _Partial(
            call_id if isinstance(call_id, str) else None,
            name if isinstance(name, str) else None,
            arguments if isinstance(arguments, str) else "",
            index,
        )

    def _finish(self, key: Any) -> None:
        partial = self.open.pop(key, None)
        if partial is not None:
            self.completed.append(partial.done())

    def finish_all(self) -> None:
        for key in list(self.open):
            self._finish(key)

    def _model(self, source: Any) -> None:
        model = _field(source, "model")
        if isinstance(model, str) and model:
            self.model = model

    # -- feeding ---------------------------------------------------------------
    def feed(self, event: Any) -> str:
        """Account for one event; return the text it adds."""
        if isinstance(event, str):
            self.committed = True
            self.text_parts.append(event)
            return event
        kind = _field(event, "type")
        if isinstance(kind, str):
            return self._typed(kind, event)
        if _field(event, "choices") is not None or _field(event, "usage") is not None:
            return self._chat(event)
        self.committed = True
        return ""

    def _typed(self, kind: str, event: Any) -> str:
        if kind not in _PRELUDE_TYPES:
            self.committed = True
        if kind in _DERIVED_TYPES:
            return ""
        if kind == "message_start":
            message = _field(event, "message")
            self._model(message)
            self.usage.update(_usage_fields(_field(message, "usage")))
        elif kind == "content_block_start":
            block = _field(event, "content_block")
            index = _int(_field(event, "index")) or 0
            block_type = _field(block, "type")
            if isinstance(block_type, str) and block_type.endswith("tool_use"):
                self._start(index, _field(block, "id"), _field(block, "name"), "", index)
                given = _field(block, "input")
                if isinstance(given, Mapping) and given:
                    self.open[index].fallback = json.dumps(dict(given))
            elif block_type == "text":
                text = _field(block, "text")
                if isinstance(text, str) and text:
                    self.text_parts.append(text)
                    return text
        elif kind == "content_block_delta":
            delta = _field(event, "delta")
            delta_type = _field(delta, "type")
            index = _int(_field(event, "index")) or 0
            if delta_type == "text_delta":
                text = _field(delta, "text")
                if isinstance(text, str):
                    self.text_parts.append(text)
                    return text
            elif delta_type == "input_json_delta" and index in self.open:
                piece = _field(delta, "partial_json")
                if isinstance(piece, str):
                    self.open[index].arguments += piece
        elif kind == "content_block_stop":
            self._finish(_int(_field(event, "index")) or 0)
        elif kind == "message_delta":
            self.usage.update(_usage_fields(_field(event, "usage")))
        elif kind == "response.output_text.delta":
            text = _field(event, "delta")
            if isinstance(text, str):
                self.text_parts.append(text)
                return text
        elif kind == "response.output_item.added":
            item = _field(event, "item")
            if _field(item, "type") == "function_call":
                index = _int(_field(event, "output_index")) or 0
                call_id = _field(item, "call_id") or _field(item, "id")
                self._start(index, call_id, _field(item, "name"), _field(item, "arguments"), index)
        elif kind == "response.function_call_arguments.delta":
            index = _int(_field(event, "output_index")) or 0
            piece = _field(event, "delta")
            if index in self.open and isinstance(piece, str):
                self.open[index].arguments += piece
        elif kind == "response.function_call_arguments.done":
            index = _int(_field(event, "output_index")) or 0
            arguments = _field(event, "arguments")
            if index in self.open and isinstance(arguments, str):
                self.open[index].arguments = arguments
        elif kind == "response.output_item.done":
            item = _field(event, "item")
            index = _int(_field(event, "output_index")) or 0
            if _field(item, "type") == "function_call":
                if index not in self.open:
                    call_id = _field(item, "call_id") or _field(item, "id")
                    self._start(index, call_id, _field(item, "name"), "", index)
                arguments = _field(item, "arguments")
                if isinstance(arguments, str) and arguments:
                    self.open[index].arguments = arguments
                self._finish(index)
        elif kind in ("response.completed", "response.incomplete"):
            response = _field(event, "response")
            self._model(response)
            self.usage.update(_usage_fields(_field(response, "usage")))
        return ""

    def _chat(self, chunk: Any) -> str:
        self._model(chunk)
        usage = _field(chunk, "usage")
        if usage is not None:
            self.usage.update(_usage_fields(usage))
        added: list[str] = []
        for choice in _field(chunk, "choices") or ():
            delta = _field(choice, "delta")
            content = _field(delta, "content")
            if isinstance(content, str) and content:
                self.committed = True
                self.text_parts.append(content)
                added.append(content)
            for call in _field(delta, "tool_calls") or ():
                self.committed = True
                index = _int(_field(call, "index")) or 0
                function = _field(call, "function")
                if index not in self.open:
                    for earlier in [key for key in self.open if key != index]:
                        self._finish(earlier)  # OpenAI streams parallel calls one by one
                    self._start(
                        index,
                        _field(call, "id"),
                        _field(function, "name"),
                        _field(function, "arguments"),
                        index,
                    )
                else:
                    piece = _field(function, "arguments")
                    if isinstance(piece, str):
                        self.open[index].arguments += piece
            if _field(choice, "finish_reason") is not None:
                self.finish_all()
        return "".join(added)

    # -- results ---------------------------------------------------------------
    @property
    def text(self) -> str:
        return "".join(self.text_parts)

    def usage_object(self) -> Usage | None:
        if not self.usage:
            return None
        return read_usage({"usage": dict(self.usage), "model": self.model})


# ------------------------------------------------------------------- resume_safe

#: Transport errors raised while a stream body is read (httpx and look-alikes).
_STREAM_ERROR_NAMES = frozenset(
    {
        "TransportError",
        "NetworkError",
        "ReadError",
        "ReadTimeout",
        "RemoteProtocolError",
        "IncompleteRead",
        "ChunkedEncodingError",
    }
)


def _guard_sync(source: Any, limits: Mapping[str, float | None], name: str) -> Iterable[Any]:
    """`stream_timeout` forced onto the sync protocol (SDK streams offer both)."""
    from python_wrap.llm import _Limits, _SyncStream

    return _SyncStream(source, _Limits(**limits), None, name)


def _guard_async(source: Any, limits: Mapping[str, float | None], name: str) -> AsyncIterable[Any]:
    from python_wrap.llm import _AsyncStream, _Limits

    return _AsyncStream(source, _Limits(**limits), None, name)


def stream_transient(exc: BaseException) -> bool:
    """`llm_transient`, plus the ways a stream fails after its HTTP 200.

    Also accepts transport errors raised while reading the body (``ReadError``,
    ``RemoteProtocolError``... by class name) and error events sent inside the stream
    whose body is typed ``overloaded_error`` / ``rate_limit_error`` / ``api_error``
    (the SDKs raise those with the stream's 200 status).
    """
    from python_wrap.llm import _body_codes, _is_refusal

    if llm_transient(exc):
        return True
    if isinstance(exc, StreamInterrupted) or _is_refusal(exc):
        return False
    if any(klass.__name__ in _STREAM_ERROR_NAMES for klass in type(exc).__mro__):
        return True
    return bool(_body_codes(exc) & {"overloaded_error", "rate_limit_error", "api_error"})


_Finish = Callable[["ResumableStream[Any]", "BaseException | None"], Any]


class _Text:
    """``stream.text_stream``: only the text pieces, sync or async."""

    def __init__(self, owner: ResumableStream[Any]) -> None:
        self._owner = owner

    def __iter__(self) -> Iterator[str]:
        for _event in self._owner:
            piece = self._owner._last_text
            if piece:
                yield piece

    async def __aiter__(self) -> AsyncIterator[str]:
        async for _event in self._owner:
            piece = self._owner._last_text
            if piece:
                yield piece


class ResumableStream(Generic[T]):
    """A streamed LLM response that reopens itself only while that is safe.

    Build it with `resume_safe`. Iterate it with ``for`` (sync streams) or ``async for``
    (async streams); it yields the provider's own events unchanged. Events that carry
    no output yet (``message_start``, ``response.created``, a role-only chat chunk) are
    held back, so when the stream fails before any output it is closed and reopened
    and the caller never sees a duplicate. Once output has been yielded (a text delta,
    a content block, a tool-call delta) a failure raises `StreamInterrupted` with what
    was received.

    Attributes:
        attempts: How many times the stream was opened.
        name: Used in events and errors.
    """

    def __init__(
        self,
        factory: Callable[[], Any],
        *,
        max_attempts: int = 3,
        retry_if: Callable[[BaseException], bool] | None = None,
        delay: float = 0.5,
        max_delay: float = 8.0,
        first_token: float | None = None,
        chunk_gap: float | None = None,
        total: float | None = None,
        on_finish: _Finish | None = None,
        name: str = "stream",
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise ValueError(f"max_attempts must be an integer, got {max_attempts!r}")
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts!r}")
        for label, value in (("delay", delay), ("max_delay", max_delay)):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{label} must be >= 0, got {value!r}")
        self._factory = factory
        self.max_attempts = max_attempts
        self._retry_if = retry_if if retry_if is not None else stream_transient
        self._delay, self._max_delay = delay, max_delay
        self._limits: dict[str, float | None] | None = None
        if first_token is not None or chunk_gap is not None or total is not None:
            stream_timeout([], first_token=first_token, chunk_gap=chunk_gap, total=total)
            self._limits = {"first_token": first_token, "chunk_gap": chunk_gap, "total": total}
        self._on_finish = on_finish
        self.name = name
        self.attempts = 0
        self._tracker = _Tracker()
        self._committed = False
        self._started = False
        self._finished = False
        self._current: Any = None
        self._wrapped: Any = None
        self._exit: Callable[..., Any] | None = None
        self._last_text = ""
        self._gen: Generator[T, None, None] | None = None
        self._agen: AsyncGenerator[T, None] | None = None

    # -- what was received -----------------------------------------------------
    @property
    def text(self) -> str:
        """The text received so far."""
        return self._tracker.text

    @property
    def tool_calls(self) -> tuple[StreamedToolCall, ...]:
        """Tool calls whose arguments are complete."""
        return tuple(self._tracker.completed)

    @property
    def pending_tool_calls(self) -> tuple[StreamedToolCall, ...]:
        """Tool calls that have started but not finished."""
        return tuple(partial.done() for partial in self._tracker.open.values())

    @property
    def usage(self) -> Usage | None:
        """Token usage the stream reported (needs the provider to send it)."""
        return self._tracker.usage_object()

    @property
    def model(self) -> str | None:
        """The model the stream reported, if any."""
        return self._tracker.model

    @property
    def usage_fields(self) -> Mapping[str, int]:
        """Raw usage counts, including cache reads and writes when reported."""
        return dict(self._tracker.usage)

    @property
    def committed(self) -> bool:
        """True once output has been handed to the caller (reopening is then unsafe)."""
        return self._committed

    @property
    def text_stream(self) -> _Text:
        """Iterate only the text pieces (``for`` or ``async for``)."""
        return _Text(self)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        current = self.__dict__.get("_current")
        if current is None:
            raise AttributeError(f"{type(self).__name__} has no attribute {name!r} (yet)")
        return getattr(current, name)

    # -- shared steps ----------------------------------------------------------
    def _wait(self, exc: BaseException) -> float:
        hint = llm_retry_after(exc)
        if hint is not None:
            return min(hint, self._max_delay)
        backoff: float = self._delay * 2.0 ** (self.attempts - 1)
        return min(backoff, self._max_delay)

    def _interrupted(self, exc: BaseException) -> StreamInterrupted:
        tracker = self._tracker
        _core.emit(
            "llm.stream_interrupted",
            self.name,
            chars=len(tracker.text),
            tool_calls=len(tracker.completed),
            exception=exc,
        )
        return StreamInterrupted(
            f"stream {self.name!r} failed after output started "
            f"({len(tracker.text)} chars, {len(tracker.completed)} complete tool calls, "
            f"{len(tracker.open)} pending); not replayed: {type(exc).__name__}",
            text=tracker.text,
            tool_calls=tuple(tracker.completed),
            pending=self.pending_tool_calls,
            attempts=self.attempts,
            usage=tracker.usage_object(),
        )

    def _gives_up(self, exc: BaseException) -> bool:
        return self.attempts >= self.max_attempts or not self._retry_if(exc)

    def _resumed(self, exc: BaseException, wait: float) -> None:
        _core.emit(
            "llm.stream_resumed", self.name, attempt=self.attempts, exception=exc, delay=wait
        )

    def _finish(self, error: BaseException | None) -> None:
        if self._finished:
            return
        self._finished = True
        if error is None:
            self._tracker.finish_all()
        if self._on_finish is not None:
            try:
                self._on_finish(self, error)
            except Exception:
                _core.get_logger().exception("python_wrap resume_safe finish hook failed")

    def _start(self) -> None:
        if self._started:
            raise RuntimeError(f"stream {self.name!r} can be iterated only once")
        self._started = True

    def _accept(
        self, tracker: _Tracker, event: Any, buffer: list[tuple[Any, str]]
    ) -> list[tuple[Any, str]]:
        """Feed ``event``; return ``(event, text)`` pairs to hand out now (none while held)."""
        item = (event, tracker.feed(event))
        if self._committed:
            return [item]
        if tracker.committed:
            self._committed = True
            self._tracker = tracker
            released = [*buffer, item]
            buffer.clear()
            return released
        buffer.append(item)
        return []

    # -- sync ------------------------------------------------------------------
    def _open(self) -> Iterable[Any]:
        made = self._factory()
        if inspect.isawaitable(made):
            close = getattr(made, "close", None)
            if callable(close):
                close()
            raise TypeError(f"stream {self.name!r} is async: iterate it with `async for`")
        self._exit = None
        if not isinstance(made, Iterable) and hasattr(made, "__enter__"):
            self._exit = made.__exit__
            made = made.__enter__()
        self._current = made
        self._wrapped = _guard_sync(made, self._limits, self.name) if self._limits else made
        return cast("Iterable[Any]", self._wrapped)

    def _close(self) -> None:
        wrapped, current, leave = self._wrapped, self._current, self._exit
        self._wrapped = self._exit = None
        if leave is not None:
            with contextlib.suppress(Exception):
                leave(None, None, None)
        for target in (wrapped, current):
            if target is None or (leave is not None and target is current):
                continue
            close = getattr(target, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    result = close()
                    if inspect.isawaitable(result):
                        cast(Any, result).close()

    def __iter__(self) -> Iterator[T]:
        self._start()
        self._gen = self._run()
        return self._gen

    def _hand_out(self, items: list[tuple[Any, str]]) -> Iterator[T]:
        for event, text in items:
            self._last_text = text
            yield cast(T, event)

    def _run(self) -> Generator[T, None, None]:
        error: BaseException | None = None
        try:
            while True:
                self.attempts += 1
                tracker = _Tracker()
                buffer: list[tuple[Any, str]] = []
                try:
                    for event in self._open():
                        yield from self._hand_out(self._accept(tracker, event, buffer))
                except Exception as exc:
                    self._close()
                    if self._committed:
                        raise self._interrupted(exc) from exc
                    if self._gives_up(exc):
                        self._tracker = tracker
                        raise
                    wait = self._wait(exc)
                    self._resumed(exc, wait)
                    _core.sleep(wait)
                    continue
                if not self._committed:
                    self._tracker = tracker
                    yield from self._hand_out(buffer)
                self._close()
                return
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._close()
            self._finish(error)

    # -- async -----------------------------------------------------------------
    async def _aopen(self) -> AsyncIterable[Any]:
        made = self._factory()
        if inspect.isawaitable(made):
            made = await made
        self._exit = None
        if not isinstance(made, AsyncIterable) and hasattr(made, "__aenter__"):
            self._exit = made.__aexit__
            made = await made.__aenter__()
        if not isinstance(made, AsyncIterable):
            raise TypeError(f"stream {self.name!r} is sync: iterate it with `for`")
        self._current = made
        self._wrapped = _guard_async(made, self._limits, self.name) if self._limits else made
        return cast("AsyncIterable[Any]", self._wrapped)

    async def _aclose(self) -> None:
        wrapped, current, leave = self._wrapped, self._current, self._exit
        self._wrapped = self._exit = None
        if leave is not None:
            with contextlib.suppress(Exception):
                await _maybe_await(leave(None, None, None))
        for target in (wrapped, current):
            if target is None or (leave is not None and target is current):
                continue
            for method in ("aclose", "close"):
                close = getattr(target, method, None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        await _maybe_await(close())
                    break

    def __aiter__(self) -> AsyncIterator[T]:
        self._start()
        self._agen = self._arun()
        return self._agen

    async def _arun(self) -> AsyncGenerator[T, None]:
        error: BaseException | None = None
        try:
            while True:
                self.attempts += 1
                tracker = _Tracker()
                buffer: list[tuple[Any, str]] = []
                try:
                    async for event in await self._aopen():
                        for item, text in self._accept(tracker, event, buffer):
                            self._last_text = text
                            yield cast(T, item)
                except Exception as exc:
                    await self._aclose()
                    if self._committed:
                        raise self._interrupted(exc) from exc
                    if self._gives_up(exc):
                        self._tracker = tracker
                        raise
                    wait = self._wait(exc)
                    self._resumed(exc, wait)
                    await _core.async_sleep(wait)
                    continue
                if not self._committed:
                    self._tracker = tracker
                    for item, text in buffer:
                        self._last_text = text
                        yield cast(T, item)
                await self._aclose()
                return
        except BaseException as exc:
            error = exc
            raise
        finally:
            await self._aclose()
            self._finish(error)

    # -- context managers ------------------------------------------------------
    def close(self) -> None:
        """Stop reading and close the underlying stream (sync)."""
        gen = self.__dict__.get("_gen")
        if gen is not None:
            gen.close()
        self._close()
        self._finish(None)

    async def aclose(self) -> None:
        """Stop reading and close the underlying stream (async)."""
        agen = self.__dict__.get("_agen")
        if agen is not None:
            await agen.aclose()
        await self._aclose()
        self._finish(None)

    def __enter__(self) -> ResumableStream[T]:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    async def __aenter__(self) -> ResumableStream[T]:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return (
            f"ResumableStream({self.name!r}, attempts={self.attempts}, committed={self._committed})"
        )


def resume_safe(
    source: Callable[..., Any] | None = None,
    /,
    *,
    max_attempts: int = 3,
    retry_if: Callable[[BaseException], bool] | None = None,
    delay: float = 0.5,
    max_delay: float = 8.0,
    first_token: float | None = None,
    chunk_gap: float | None = None,
    total: float | None = None,
    name: str | None = None,
) -> Any:
    """Reopen a failed LLM stream only when no output has been handed out yet.

    Retrying a stream is not like retrying a call. A failure before the first output
    (connection reset, 529 on open, a stall before the first token) is safe to retry:
    the stream is closed, the wait doubles from ``delay`` (or follows ``Retry-After``)
    and it is opened again, invisibly to the caller. A failure after text or a tool
    call was yielded is not: replaying would duplicate the text, or start a tool call
    the caller may already be acting on. Then `StreamInterrupted` is raised, carrying
    the partial text, the tool calls that completed and the ones still pending.

    Events are read by shape: OpenAI chat-completion chunks (``choices[].delta``),
    OpenAI Responses events (``response.output_text.delta``,
    ``response.function_call_arguments.delta``, ``response.output_item.done``...),
    Anthropic message events (``content_block_start``, ``content_block_delta``,
    ``content_block_stop``...), python-wrap's `FakeStream` chunks and plain strings.

    Two forms:

    - ``resume_safe(lambda: client.chat.completions.create(..., stream=True))``: a
      zero-argument factory that opens the stream (sync, async, or returning a context
      manager such as Anthropic's ``messages.stream(...)``).
    - ``@resume_safe(...)`` on a function returning a stream; each call returns a
      `ResumableStream` that calls the function again to reopen.

    Args:
        source: The factory, or None for the decorator form.
        max_attempts: Total opens, including the first.
        retry_if: Which errors before output are worth reopening for;
            `stream_transient` by default (`llm_transient`, which accepts
            `StreamTimeout`, plus transport errors and in-stream overloaded errors).
        delay: First wait between opens, doubled each time.
        max_delay: Longest wait, also the cap on ``Retry-After`` hints.
        first_token: Stall limit for the first event of each open (`stream_timeout`).
        chunk_gap: Stall limit between events.
        total: Limit for one open, start to end.
        name: Used in events and errors.

    Returns:
        A `ResumableStream`, or a decorator.

    Example:
        ```python
        from python_wrap.adapters import StreamInterrupted, resume_safe
        from python_wrap.llm_testing import FakeStream
        from python_wrap.testing import fake_clock


        class Reset(ConnectionError):
            pass


        opened = []


        def open_stream() -> FakeStream:
            opened.append(1)
            if len(opened) == 1:  # the first open fails before any output: reopened
                return FakeStream(["Hel", "lo"], error=Reset, error_after=0)
            return FakeStream(["Hel", "lo"], error=Reset, error_after=1)  # fails mid-way

        stream = resume_safe(open_stream)
        received = []
        with fake_clock():
            try:
                for chunk in stream:
                    received.append(chunk.text)
            except StreamInterrupted as exc:
                assert exc.text == "Hel" and exc.attempts == 2
        assert received == ["Hel"] and len(opened) == 2
        ```
    """
    options: dict[str, Any] = {
        "max_attempts": max_attempts,
        "retry_if": retry_if,
        "delay": delay,
        "max_delay": max_delay,
        "first_token": first_token,
        "chunk_gap": chunk_gap,
        "total": total,
    }
    if source is not None:
        label = name or _core.qualname(source)
        return ResumableStream(source, name=label, **options)
    ResumableStream(lambda: (), **options)  # validate now, not on the first call
    limited = first_token is not None or chunk_gap is not None or total is not None
    settings = {
        "llm": "resume_safe",
        "max_attempts": max_attempts,
        "first_token": first_token,
        "chunk_gap": chunk_gap,
        "total": total,
        "seconds": total,
    }

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        label = name or _core.qualname(func)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> ResumableStream[Any]:
            return ResumableStream(lambda: func(*args, **kwargs), name=label, **options)

        setattr(wrapper, _LAYER_ATTR, ("timeout" if limited else "guard", settings))
        return wrapper

    setattr(decorate, _LAYER_ATTR, ("timeout" if limited else "guard", settings))
    return decorate


# ------------------------------------------------------------------- CacheGuard


class PromptCacheWarning(UserWarning):
    """Prompt caching looks ineffective (`CacheGuard`)."""


@dataclasses.dataclass(frozen=True)
class CacheStats:
    """What a `CacheGuard` has seen.

    Attributes:
        calls: Responses observed.
        eligible_calls: Those with a prompt long enough to be cached.
        prompt_tokens: Prompt tokens of the eligible calls (cached or not).
        cache_read_tokens: Tokens served from the cache.
        cache_write_tokens: Tokens written to the cache (Anthropic ``cache_creation``).
    """

    calls: int = 0
    eligible_calls: int = 0
    prompt_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def hit_rate(self) -> float:
        """Share of eligible prompt tokens read from the cache (0.0 to 1.0)."""
        return self.cache_read_tokens / self.prompt_tokens if self.prompt_tokens else 0.0


_VOLATILE = (
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
    ("timestamp", re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")),
    ("time of day", re.compile(r"\b\d{1,2}:\d{2}:\d{2}\b")),
    ("unix timestamp", re.compile(r"\b1\d{9}(?:\d{3})?\b")),
    ("random id", re.compile(r"\b[0-9a-f]{24,}\b", re.I)),
)


def _texts(value: Any, depth: int = 0) -> Iterator[str]:
    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key in ("text", "content", "description", "name", "instructions"):
            if key in value:
                yield from _texts(value[key], depth + 1)
        if "input_schema" in value or "parameters" in value or "function" in value:
            for key in ("input_schema", "parameters", "function"):
                if key in value:
                    yield json.dumps(value[key], sort_keys=True, default=str)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _texts(item, depth + 1)
    else:
        text = getattr(value, "text", None)
        if isinstance(text, str):
            yield text


def prompt_prefix(request: Mapping[str, Any]) -> str:
    """The stable part of a request a prompt cache keys on: tools, system, instructions.

    Covers Anthropic ``system=`` (string or blocks) and ``tools=``, OpenAI ``tools=``,
    ``instructions=`` and the leading ``system``/``developer`` messages.
    """
    parts: list[str] = []
    parts += _texts(request.get("tools"))
    parts += _texts(request.get("system"))
    parts += _texts(request.get("instructions"))
    for message in request.get("messages") or ():
        role = _field(message, "role")
        if role not in ("system", "developer"):
            break
        parts += _texts(_field(message, "content"))
    leading = request.get("input")
    if isinstance(leading, (list, tuple)):
        for message in leading:
            if _field(message, "role") not in ("system", "developer"):
                break
            parts += _texts(_field(message, "content"))
    return "\n".join(parts)


def volatile_kinds(text: str) -> tuple[str, ...]:
    """Kinds of per-request values (``"timestamp"``, ``"uuid"``...) found in ``text``."""
    return tuple(kind for kind, pattern in _VOLATILE if pattern.search(text))


def _has_cache_control(value: Any, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(value, Mapping):
        if "cache_control" in value:
            return True
        return any(_has_cache_control(item, depth + 1) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_cache_control(item, depth + 1) for item in value)
    return False


def _cache_numbers(response: Any) -> tuple[int, int, int, bool] | None:
    """``(prompt_tokens, cache_read, cache_write, anthropic_style)`` from a response."""
    raw = _field(response, "usage")
    if raw is None:
        raw = response
    fields = _usage_fields(raw)
    anthropic = any(
        key in fields or _has_attr(raw, key)
        for key in ("cache_creation_input_tokens", "cache_read_input_tokens")
    )
    if anthropic:
        read = fields.get("cache_read_input_tokens", 0)
        write = fields.get("cache_creation_input_tokens", 0)
        return fields.get("input_tokens", 0) + read + write, read, write, True
    prompt = fields.get("prompt_tokens", fields.get("input_tokens"))
    if prompt is None:
        return None
    return prompt, min(fields.get("cached_tokens", 0), prompt), 0, False


def _has_attr(raw: Any, name: str) -> bool:
    if isinstance(raw, Mapping):
        return name in raw
    return raw is not None and hasattr(raw, name)


class CacheGuard:
    """Watch prompt-cache effectiveness and flag prompts that defeat the cache.

    Prompt caching (Anthropic ``cache_control`` breakpoints, OpenAI's automatic prefix
    cache) only pays when the prefix (tools, system prompt, leading messages) is byte
    for byte the same on every call. A timestamp or a request id in the system prompt
    silently turns every call into a cache miss at full price.

    `observe` each response (the guarded clients do it for you) with its request. The
    guard adds up cache reads and writes from the usage
    (``cache_read_input_tokens`` / ``cache_creation_input_tokens`` or
    ``prompt_tokens_details.cached_tokens``) and, once ``min_calls`` calls with a
    cacheable prompt have a hit rate at or below ``min_hit_rate``, warns once
    (`PromptCacheWarning` and an ``llm.prompt_cache_cold`` event) with the likely
    reasons. A prefix containing a timestamp, UUID or long random id is reported once
    as soon as it is seen (``llm.prompt_cache_volatile``). Only hashes and the kind of
    value are kept, never prompt text.

    Args:
        min_calls: Eligible calls before the hit rate is judged.
        min_hit_rate: Hit rate at or below which the cache counts as cold.
        min_prompt_tokens: Prompts shorter than this are not cacheable (1024 tokens on
            most models) and are not counted.
        name: Used in warnings and events.
        warn: Also call `warnings.warn` (events are always emitted).

    Example:
        ```python
        import warnings

        from python_wrap.adapters import CacheGuard, PromptCacheWarning

        guard = CacheGuard(min_calls=3, min_prompt_tokens=100)
        request = {"system": "You are a helpful assistant.", "messages": []}
        usage = {"usage": {"input_tokens": 2000, "output_tokens": 10,
                           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            for _ in range(3):
                guard.observe(usage, request)
        assert guard.stats().hit_rate == 0.0
        assert [w.category for w in caught] == [PromptCacheWarning]
        assert "cache_control" in str(caught[0].message)
        ```
    """

    def __init__(
        self,
        *,
        min_calls: int = 5,
        min_hit_rate: float = 0.05,
        min_prompt_tokens: int = 1024,
        name: str = "prompt-cache",
        warn: bool = True,
    ) -> None:
        if isinstance(min_calls, bool) or not isinstance(min_calls, int) or min_calls < 1:
            raise ValueError(f"min_calls must be an integer >= 1, got {min_calls!r}")
        if not 0 <= min_hit_rate < 1:
            raise ValueError(f"min_hit_rate must be in [0, 1), got {min_hit_rate!r}")
        if min_prompt_tokens < 0:
            raise ValueError(f"min_prompt_tokens must be >= 0, got {min_prompt_tokens!r}")
        self.min_calls, self.min_hit_rate = min_calls, min_hit_rate
        self.min_prompt_tokens, self.name, self.warn = min_prompt_tokens, name, warn
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        """Forget everything seen and re-arm the warnings."""
        with self._lock:
            self._stats = CacheStats()
            self._prefixes: set[str] = set()
            self._with_prefix = 0
            self._volatile: set[str] = set()
            self._marked = False
            self._anthropic = False
            self._cold_warned = False
            self._volatile_warned = False

    def stats(self) -> CacheStats:
        """Totals so far."""
        with self._lock:
            return self._stats

    @property
    def volatile(self) -> tuple[str, ...]:
        """Kinds of per-request values seen in a prefix so far."""
        with self._lock:
            return tuple(sorted(self._volatile))

    def check_prefix(self, request: Mapping[str, Any]) -> tuple[str, ...]:
        """Kinds of per-request values in the request's cacheable prefix (no recording)."""
        return volatile_kinds(prompt_prefix(request))

    def _reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self._volatile:
            kinds = ", ".join(sorted(self._volatile))
            reasons.append(f"the prompt prefix contains values that change per call ({kinds})")
        if self._with_prefix >= 2 and len(self._prefixes) == self._with_prefix:
            reasons.append("the prompt prefix (tools, system prompt) differed on every call")
        if self._anthropic and not self._marked:
            reasons.append(
                "no cache_control breakpoint in the requests "
                "(Anthropic caches only marked prefixes)"
            )
        return tuple(reasons)

    def observe(self, response: Any, request: Mapping[str, Any] | None = None) -> CacheStats:
        """Record one response (and the request that produced it).

        Args:
            response: A response with ``usage``, a usage object or a usage mapping.
            request: The request keyword arguments, to inspect the prefix.

        Returns:
            The updated totals.
        """
        numbers = _cache_numbers(response)
        volatile: tuple[str, ...] = ()
        prefix_hash: str | None = None
        marked = False
        if request is not None:
            prefix = prompt_prefix(request)
            if prefix:
                volatile = volatile_kinds(prefix)
                prefix_hash = hashlib.sha256(prefix.encode()).hexdigest()
            marked = _has_cache_control(request) or "cache_control" in request
        fire_volatile: tuple[str, ...] | None = None
        fire_cold: tuple[float, int, tuple[str, ...]] | None = None
        with self._lock:
            stats = self._stats
            calls = stats.calls + 1
            eligible, prompt, read, write = (
                stats.eligible_calls,
                stats.prompt_tokens,
                stats.cache_read_tokens,
                stats.cache_write_tokens,
            )
            if numbers is not None and numbers[0] >= self.min_prompt_tokens:
                eligible += 1
                prompt += numbers[0]
                read += numbers[1]
                write += numbers[2]
                self._anthropic = self._anthropic or numbers[3]
                if prefix_hash is not None:
                    self._prefixes.add(prefix_hash)
                    self._with_prefix += 1
                self._marked = self._marked or marked
            self._stats = CacheStats(calls, eligible, prompt, read, write)
            if volatile and not self._volatile.issuperset(volatile):
                self._volatile.update(volatile)
                if not self._volatile_warned:
                    self._volatile_warned = True
                    fire_volatile = volatile
            stats = self._stats
            if (
                not self._cold_warned
                and stats.eligible_calls >= self.min_calls
                and stats.hit_rate <= self.min_hit_rate
            ):
                self._cold_warned = True
                fire_cold = (stats.hit_rate, stats.eligible_calls, self._reasons())
        if fire_volatile is not None:
            _core.emit("llm.prompt_cache_volatile", self.name, kinds=fire_volatile)
            self._warn(
                f"{self.name}: the cached prompt prefix contains {', '.join(fire_volatile)}; "
                "a value that changes per call makes every call a cache miss. Move it after "
                "the cached prefix (into the last user message)."
            )
        if fire_cold is not None:
            rate, count, reasons = fire_cold
            _core.emit(
                "llm.prompt_cache_cold", self.name, calls=count, hit_rate=rate, reasons=reasons
            )
            why = "; ".join(reasons) if reasons else "the prefix may be shorter than the minimum"
            self._warn(
                f"{self.name}: prompt-cache hit rate is {rate:.0%} over {count} cacheable "
                f"calls. Likely: {why}."
            )
        return stats

    def _warn(self, message: str) -> None:
        if self.warn:
            warnings.warn(message, PromptCacheWarning, stacklevel=4)

    def __repr__(self) -> str:
        stats = self.stats()
        return f"CacheGuard({self.name!r}, calls={stats.calls}, hit_rate={stats.hit_rate:.2f})"


# ------------------------------------------------------------------ the proxy


def _is_async_client(client: Any) -> bool:
    return type(client).__name__.startswith("Async")


class _Endpoint:
    """One guarded SDK resource (``chat.completions``, ``responses``, ``messages``)."""

    def __init__(self, guard: GuardedClient, path: tuple[str, ...], kind: str) -> None:
        self._guard = guard
        self._path = path
        self._kind = kind
        self._label = f"{guard.system}.{'.'.join(path)}"
        create: Any
        if guard.is_async:
            self._call = guard._compose(self._label, self._ainvoke)

            async def acreate(*args: Any, **kwargs: Any) -> Any:
                if kwargs.get("stream"):
                    return await self._astream(args, kwargs)
                return await self._call(*args, **kwargs)

            create = acreate
        else:
            self._call = guard._compose(self._label, self._invoke)

            def screate(*args: Any, **kwargs: Any) -> Any:
                if kwargs.get("stream"):
                    return self._stream(args, kwargs)
                return self._call(*args, **kwargs)

            create = screate
        create.__wrapped__ = self._call  # so doctor() sees the layers
        create.__qualname__ = create.__name__ = f"{self._label}.create"
        self.create = create
        if kind == "messages":
            self.stream = self._stream_manager

    def _target(self, client: Any | None = None) -> Any:
        target = self._guard.client if client is None else client
        for part in self._path:
            target = getattr(target, part)
        return target

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._target(self._guard.raw), name)

    # -- the HTTP call, reading headers when the SDK exposes them ---------------
    def _raw_target(self) -> tuple[Any, Any]:
        target = self._target()
        raw = getattr(target, "with_raw_response", None) if self._guard.read_headers else None
        return target, raw

    def _send(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> tuple[Any, Any]:
        target, raw = self._raw_target()
        if raw is not None:
            response = raw.create(*args, **kwargs)
            return response.parse(), _headers_of(response)
        return target.create(*args, **kwargs), None

    async def _asend(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> tuple[Any, Any]:
        target, raw = self._raw_target()
        if raw is not None:
            response = await _maybe_await(raw.create(*args, **kwargs))
            return await _maybe_await(response.parse()), _headers_of(response)
        return await _maybe_await(target.create(*args, **kwargs)), None

    # -- one attempt of a plain call --------------------------------------------
    def _invoke(self, *args: Any, **kwargs: Any) -> Any:
        guard = self._guard
        cost = guard._reserve(args, kwargs, self._label)
        try:
            result, headers = self._send(args, kwargs)
        except BaseException as exc:
            guard._failed(cost, exc)
            raise
        guard._settled(cost, result, headers, kwargs)
        return result

    async def _ainvoke(self, *args: Any, **kwargs: Any) -> Any:
        guard = self._guard
        cost = await guard._areserve(args, kwargs, self._label)
        try:
            result, headers = await self._asend(args, kwargs)
        except BaseException as exc:
            guard._failed(cost, exc)
            raise
        guard._settled(cost, result, headers, kwargs)
        return result

    # -- streams: one admission, reopened by ResumableStream ---------------------
    def _stream_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        kwargs = dict(kwargs)
        if self._kind == "chat" and "stream_options" not in kwargs:
            kwargs["stream_options"] = {"include_usage": True}
        return kwargs

    def _open(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
        try:
            stream, headers = self._send(args, kwargs)
        except BaseException as exc:
            self._guard._seed_error(exc)
            raise
        self._guard._seed(headers)
        return stream

    async def _aopen(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> Any:
        try:
            stream, headers = await self._asend(args, kwargs)
        except BaseException as exc:
            self._guard._seed_error(exc)
            raise
        self._guard._seed(headers)
        return stream

    def _stream(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> ResumableStream[Any]:
        guard = self._guard
        kwargs = self._stream_kwargs(kwargs)
        admission = guard._admit(args, kwargs, self._label)
        return guard._resumable(self._label, lambda: self._open(args, kwargs), admission, kwargs)

    async def _astream(self, args: tuple[Any, ...], kwargs: dict[str, Any]) -> ResumableStream[Any]:
        guard = self._guard
        kwargs = self._stream_kwargs(kwargs)
        admission = await guard._aadmit(args, kwargs, self._label)
        return guard._resumable(self._label, lambda: self._aopen(args, kwargs), admission, kwargs)

    def _stream_manager(self, *args: Any, **kwargs: Any) -> _StreamManager:
        """``messages.stream(...)``: a context manager yielding a `ResumableStream`."""
        return _StreamManager(self, args, kwargs)


class _Namespace:
    """A guarded attribute group (``guarded.chat``): named children, the rest passed on."""

    def __init__(self, guard: GuardedClient, path: tuple[str, ...], **children: Any) -> None:
        self._guard, self._path = guard, path
        for name, child in children.items():
            setattr(self, name, child)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        target = self._guard.raw
        for part in self._path:
            target = getattr(target, part)
        return getattr(target, name)


class _StreamManager:
    """``with guarded.messages.stream(...) as stream:`` (or ``async with``)."""

    def __init__(self, endpoint: _Endpoint, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self._endpoint = endpoint
        self._args, self._kwargs = args, kwargs
        self._stream: ResumableStream[Any] | None = None

    def _open(self) -> Any:
        endpoint, args, kwargs = self._endpoint, self._args, self._kwargs
        return endpoint._target().stream(*args, **kwargs)

    def __enter__(self) -> ResumableStream[Any]:
        guard, label = self._endpoint._guard, self._endpoint._label
        scope = guard._admit(self._args, self._kwargs, label)
        self._stream = guard._resumable(label, self._open, scope, self._kwargs)
        return self._stream

    def __exit__(self, *exc: object) -> None:
        if self._stream is not None:
            self._stream.close()

    async def __aenter__(self) -> ResumableStream[Any]:
        guard, label = self._endpoint._guard, self._endpoint._label
        scope = await guard._aadmit(self._args, self._kwargs, label)
        self._stream = guard._resumable(label, self._open, scope, self._kwargs)
        return self._stream

    async def __aexit__(self, *exc: object) -> None:
        if self._stream is not None:
            await self._stream.aclose()


class _Runner:
    """Per-step accounting around an SDK tool runner (sync or async)."""

    def __init__(self, guard: GuardedClient, runner: Any, kwargs: dict[str, Any], label: str):
        self._guard, self._runner, self._kwargs, self._label = guard, runner, kwargs, label
        self.steps = 0

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._runner, name)

    def __iter__(self) -> Iterator[Any]:
        guard, iterator = self._guard, iter(self._runner)
        while True:
            scope = guard._admit((), self._kwargs, self._label)
            try:
                message = next(iterator)
            except StopIteration:
                guard._release(scope)
                return
            except BaseException as exc:
                guard._release(scope, exc)
                raise
            self.steps += 1
            guard._account(scope, message, self._kwargs)
            yield message

    async def __aiter__(self) -> AsyncIterator[Any]:
        guard, iterator = self._guard, self._runner.__aiter__()
        while True:
            scope = await guard._aadmit((), self._kwargs, self._label)
            try:
                message = await iterator.__anext__()
            except StopAsyncIteration:
                guard._release(scope)
                return
            except BaseException as exc:
                guard._release(scope, exc)
                raise
            self.steps += 1
            guard._account(scope, message, self._kwargs)
            yield message

    def until_done(self) -> Any:
        """Run every step (accounted) and return the last message."""
        if self._guard.is_async:
            return self._auntil_done()
        last = None
        for message in self:
            last = message
        return last

    async def _auntil_done(self) -> Any:
        last = None
        async for message in self:
            last = message
        return last


@dataclasses.dataclass
class _Admission:
    """What `GuardedClient._admit` reserved for one streamed call or runner step."""

    scope: str | None
    cost: int


class GuardedClient:
    """The proxy `guard_openai` and `guard_anthropic` return.

    Guarded resources run each call through, outermost first: tracing
    (`traced_llm`, when ``trace`` is set), the `Budget` (checked before, charged with
    the response's usage after), the retry loop (`llm_retry` with
    ``owns_retries=True``), your per-attempt ``policy`` (a timeout, a breaker), and the
    `TokenRateLimiter` (reserve an estimate, settle with the real usage, then lower the
    bucket to the provider's ``*-remaining-tokens`` header; a 429's ``retry-after``
    drains it). Streams (``stream=True``) become `ResumableStream` objects whose usage
    is charged when they end. Every other attribute is the original client's,
    unguarded.

    Attributes:
        raw: The client you passed (its SDK retries untouched).
        client: The client calls go through (``max_retries=0`` when python-wrap owns
            retries).
        system: ``"openai"`` or ``"anthropic"``.
        is_async: Whether the client is async.
    """

    def __init__(
        self,
        client: Any,
        *,
        system: str,
        budget: Budget | None = None,
        limiter: TokenRateLimiter | None = None,
        policy: Callable[[Callable[..., Any]], Callable[..., Any]] | None = None,
        attempts: int = 4,
        retry: Mapping[str, Any] | None = None,
        owns_retries: bool = True,
        trace: bool | Mapping[str, Any] = False,
        cache_guard: CacheGuard | None = None,
        read_headers: bool = True,
        first_token: float | None = None,
        chunk_gap: float | None = None,
        total: float | None = None,
        is_async: bool | None = None,
    ) -> None:
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            raise ValueError(f"attempts must be an integer >= 1, got {attempts!r}")
        if retry is not None and ("max_attempts" in retry or "owns_retries" in retry):
            raise ValueError("pass attempts= and owns_retries= directly, not inside retry=")
        self.raw = client
        self.client = client
        if owns_retries:
            try:
                self.client = no_sdk_retries(client)
            except TypeError:
                if sdk_max_retries(client):  # it retries and cannot be told not to
                    raise
                # a client without its own retry loop (a fake, a thin wrapper): use as is
        self.system = system
        self.budget, self.limiter, self.policy = budget, limiter, policy
        self.attempts = attempts if owns_retries else 1
        self.owns_retries = owns_retries
        self._retry_options = dict(retry or {})
        self.trace: dict[str, Any] | None = None
        if isinstance(trace, Mapping):
            self.trace = dict(trace)
        elif trace:
            self.trace = {}
        self.cache_guard = cache_guard
        self.read_headers = read_headers
        self._limits = {"first_token": first_token, "chunk_gap": chunk_gap, "total": total}
        if any(value is not None for value in self._limits.values()):
            stream_timeout([], **self._limits)  # validates
        self.is_async = _is_async_client(client) if is_async is None else is_async

    # -- composition -------------------------------------------------------------
    def _compose(self, label: str, attempt: Callable[..., Any]) -> Callable[..., Any]:
        if self.is_async:

            async def one(*args: Any, **kwargs: Any) -> Any:
                return await attempt(*args, **kwargs)

            call: Callable[..., Any] = one
        else:

            def sync_one(*args: Any, **kwargs: Any) -> Any:
                return attempt(*args, **kwargs)

            call = sync_one
        call.__name__ = call.__qualname__ = label
        if self.policy is not None:
            call = self.policy(call)
        if self.owns_retries and self.attempts > 1:
            call = llm_retry(
                max_attempts=self.attempts,
                owns_retries=True,
                client=self.client,
                **self._retry_options,
            )(call)
        if self.budget is not None:
            call = self.budget(call)
        if self.trace is not None:
            from python_wrap.genai import traced_llm

            call = traced_llm(**{"system": self.system, **self.trace})(call)
        return call

    # -- limiter and budget steps ------------------------------------------------
    def _estimate(self, args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> int:
        limiter = self.limiter
        if limiter is None or _core.overrides.disable_rate_limits:
            return 0
        return max(0, int(limiter._estimate(*args, **kwargs)))

    def _reserve(self, args: tuple[Any, ...], kwargs: Mapping[str, Any], label: str) -> int:
        cost = self._estimate(args, kwargs)
        if self.limiter is not None and not _core.overrides.disable_rate_limits:
            self.limiter.acquire(cost, label=label)
        return cost

    async def _areserve(self, args: tuple[Any, ...], kwargs: Mapping[str, Any], label: str) -> int:
        cost = self._estimate(args, kwargs)
        if self.limiter is not None and not _core.overrides.disable_rate_limits:
            await self.limiter.aacquire(cost, label=label)
        return cost

    def _limiting(self) -> TokenRateLimiter | None:
        if self.limiter is None or _core.overrides.disable_rate_limits:
            return None
        return self.limiter

    def _seed(self, headers: Any) -> None:
        limiter = self._limiting()
        if limiter is not None:
            seed_limiter(limiter, headers)

    def _seed_error(self, exc: BaseException) -> None:
        limiter = self._limiting()
        if limiter is None:
            return
        status = _int(_field(exc, "status_code")) or _int(
            _field(getattr(exc, "response", None), "status_code")
        )
        hint = llm_retry_after(exc) if status == 429 else None
        seed_limiter(limiter, _headers_of(exc), retry_after=hint)

    def _failed(self, cost: int, exc: BaseException) -> None:
        limiter = self._limiting()
        if limiter is not None:
            limiter.settle(cost, 0)
            self._seed_error(exc)

    def _settled(self, cost: int, result: Any, headers: Any, kwargs: Mapping[str, Any]) -> None:
        limiter = self._limiting()
        if limiter is not None:
            usage = read_usage(result)
            limiter.settle(cost, cost if usage is None else usage.total_tokens)
            seed_limiter(limiter, headers)
        if self.cache_guard is not None:
            self.cache_guard.observe(result, kwargs)

    def _scope(self, args: tuple[Any, ...], kwargs: Mapping[str, Any], label: str) -> str | None:
        budget = self.budget
        if budget is None:
            return None
        from python_wrap.llm import _MISSING

        scope = budget._scope_key(args, dict(kwargs), _MISSING)
        budget._check(scope, label)
        return scope

    def _admit(self, args: tuple[Any, ...], kwargs: Mapping[str, Any], label: str) -> _Admission:
        scope = self._scope(args, kwargs, label)
        return _Admission(scope, self._reserve(args, kwargs, label))

    async def _aadmit(
        self, args: tuple[Any, ...], kwargs: Mapping[str, Any], label: str
    ) -> _Admission:
        scope = self._scope(args, kwargs, label)
        return _Admission(scope, await self._areserve(args, kwargs, label))

    def _release(self, admission: _Admission, error: BaseException | None = None) -> None:
        """Refund a reservation whose call produced nothing."""
        if error is not None:
            self._failed(admission.cost, error)
        else:
            limiter = self._limiting()
            if limiter is not None:
                limiter.settle(admission.cost, 0)

    def _account(self, admission: _Admission, response: Any, request: Mapping[str, Any]) -> None:
        """Charge a finished call or step: budget, limiter, cache guard."""
        usage = read_usage(response)
        if self.budget is not None and admission.scope is not None:
            self.budget._add(admission.scope, usage)
        limiter = self._limiting()
        if limiter is not None:
            limiter.settle(admission.cost, admission.cost if usage is None else usage.total_tokens)
        if self.cache_guard is not None:
            self.cache_guard.observe(response, request)

    def _resumable(
        self,
        label: str,
        factory: Callable[[], Any],
        admission: _Admission,
        request: Mapping[str, Any],
    ) -> ResumableStream[Any]:
        def finish(stream: ResumableStream[Any], error: BaseException | None) -> None:
            fields = stream.usage_fields
            if fields:
                self._account(admission, {"usage": dict(fields), "model": stream.model}, request)
            elif not stream.committed:  # nothing was produced: refund the reservation
                self._release(admission)
            # output without reported usage: the estimate stands

        options = self._retry_options
        extra = options.get("retry_if")

        def worth_reopening(exc: BaseException) -> bool:
            return stream_transient(exc) and (extra is None or bool(extra(exc)))

        return ResumableStream(
            factory,
            max_attempts=self.attempts,
            retry_if=worth_reopening,
            delay=options.get("delay", 0.5),
            max_delay=options.get("max_delay", 8.0),
            first_token=self._limits["first_token"],
            chunk_gap=self._limits["chunk_gap"],
            total=self._limits["total"],
            on_finish=finish,
            name=label,
        )

    # -- diagnostics -------------------------------------------------------------
    def doctor(self) -> Report:
        """Lint the per-call stack of this guard (`python_wrap.doctor.doctor`)."""
        from python_wrap.doctor import doctor

        return doctor(self._primary().create)

    def _primary(self) -> _Endpoint:
        raise NotImplementedError

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.raw, name)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}({type(self.raw).__name__}, attempts={self.attempts}, "
            f"budget={self.budget!r}, limiter={self.limiter!r})"
        )


# -------------------------------------------------------------------- doctor


def doctor_findings(layers: Sequence[Layer]) -> list[Finding]:
    """PW018-PW019: SDK retries under a python-wrap retry; retried streams."""
    from python_wrap.doctor import Finding

    found: list[Finding] = []
    for layer in layers:
        sdk = layer.settings.get("sdk_retries")
        if layer.role == "retry" and isinstance(sdk, int) and sdk > 0:
            attempts = layer.settings.get("max_attempts")
            total = (
                f"{attempts} x {sdk + 1} = {attempts * (sdk + 1)}"
                if isinstance(attempts, int)
                else f"{sdk + 1}x"
            )
            found.append(
                Finding(
                    "PW018",
                    "warning",
                    f"python-wrap retries an SDK client that still retries by itself "
                    f"(max_retries={sdk}): up to {total} HTTP requests per call, and the "
                    "SDK's waits ignore the RetryBudget. Pass no_sdk_retries(client) (or "
                    "use guard_openai/guard_anthropic, which do it).",
                )
            )
    kinds = [layer.settings.get("llm") for layer in layers]
    if (
        any(layer.role == "retry" for layer in layers)
        and "stream_timeout" in kinds
        and "resume_safe" not in kinds
    ):
        found.append(
            Finding(
                "PW019",
                "warning",
                "a retry wraps a streamed LLM call without resume_safe: the retry only "
                "covers opening the stream, and replaying it after output started repeats "
                "text and half-done tool calls. Use resume_safe(...) for the stream.",
            )
        )
    return found
