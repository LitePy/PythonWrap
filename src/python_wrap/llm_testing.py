"""Test doubles for LLM-calling code: a scripted fake provider and LLM-shaped faults.

Two pieces work together with `python_wrap.chaos` and `python_wrap.testing.fake_clock`:

* `FakeProvider` (built by `fake_llm`) is a deterministic stand-in for a model API. It plays
  back a script of replies, streams them in chunks with configurable delays, records every
  call, reports token usage in Anthropic style (``input_tokens``/``output_tokens``) or OpenAI
  style (``prompt_tokens``/``completion_tokens``) and can raise scripted errors.
* ``chaos.llm`` (an `LLMFaults`) adds LLM-specific faults to the ordinary fault injector:
  rate limits with ``Retry-After``, overloaded (529) and server errors, timeouts, truncated
  JSON, slow first tokens, stalled streams, tool calls with invalid arguments and
  content-policy refusals. Rules take the usual ``name``/``rate``/``times`` arguments and can
  be loaded from ``PW_CHAOS`` with an ``"llm"`` key.

No provider SDK is imported anywhere. The errors raised here are duck-typed look-alikes
(``status_code``, ``response.status_code``, familiar class names), so retry and
classification code that matches errors by those attributes treats them like the real thing.

All waiting goes through `python_wrap._core`, so under ``fake_clock`` stream delays advance
the fake clock and a test finishes in milliseconds.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import SimpleNamespace, TracebackType
from typing import TYPE_CHECKING, Any

from python_wrap import _core
from python_wrap.chaos import FaultInjector, fault_point

if TYPE_CHECKING:
    from python_wrap.chaos import Fault

__all__ = [
    "APIConnectionError",
    "APIStatusError",
    "APITimeoutError",
    "FakeCall",
    "FakeChunk",
    "FakeProvider",
    "FakeProviderExhausted",
    "FakeResponse",
    "FakeStream",
    "InternalServerError",
    "LLMError",
    "LLMFaults",
    "OverloadedError",
    "RateLimitError",
    "ToolCall",
    "fake_llm",
]

# ------------------------------------------------------------------------------ errors


class LLMError(Exception):
    """Base class of the look-alike provider errors raised by the fakes."""


class _FakeHTTPResponse:
    def __init__(self, status_code: int, headers: Mapping[str, str]) -> None:
        self.status_code = status_code
        self.headers = dict(headers)


class APIStatusError(LLMError):
    """An HTTP error status from a model API.

    Attributes:
        status_code: The HTTP status.
        response: A minimal object with ``status_code`` and ``headers``.
        retry_after: Seconds from a ``Retry-After`` header, or ``None``.
        body: The decoded error body.
    """

    default_status = 500

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
    ) -> None:
        code = self.default_status if status_code is None else status_code
        super().__init__(message or f"Error code: {code}")
        self.message = message or f"Error code: {code}"
        self.status_code = code
        self.retry_after = retry_after
        merged = dict(headers or {})
        if retry_after is not None:
            merged.setdefault("retry-after", f"{retry_after:g}")
        self.response = _FakeHTTPResponse(code, merged)
        self.body = body


class RateLimitError(APIStatusError):
    """HTTP 429, optionally with a ``Retry-After`` hint."""

    default_status = 429


class OverloadedError(APIStatusError):
    """HTTP 529: the service is overloaded."""

    default_status = 529


class InternalServerError(APIStatusError):
    """HTTP 5xx server error (500 by default)."""

    default_status = 500


class APITimeoutError(LLMError, TimeoutError):
    """The request or stream timed out."""


class APIConnectionError(LLMError, ConnectionError):
    """The connection to the API failed."""


class FakeProviderExhausted(LLMError, LookupError):
    """A `FakeProvider` was called more often than its script allows."""


# ---------------------------------------------------------------------------- responses


def make_usage(style: str, input_tokens: int, output_tokens: int) -> Any:
    """A usage object in ``"anthropic"`` or ``"openai"`` style."""
    if style == "anthropic":
        return SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    if style == "openai":
        return SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        )
    raise ValueError("style must be 'anthropic' or 'openai'")


def _tokens(usage: Any) -> tuple[int, int]:
    for a, b in (("input_tokens", "output_tokens"), ("prompt_tokens", "completion_tokens")):
        if isinstance(usage, Mapping):
            if a in usage:
                return int(usage[a]), int(usage.get(b, 0))
        elif hasattr(usage, a):
            return int(getattr(usage, a)), int(getattr(usage, b, 0))
    return 0, 0


def _estimate(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


@dataclass(frozen=True)
class ToolCall:
    """A model-requested tool call; ``arguments`` is the raw string as sent."""

    name: str
    arguments: str
    id: str = "call_1"

    @property
    def input(self) -> dict[str, Any]:
        """Parsed arguments, or ``{}`` when they are not a JSON object."""
        try:
            parsed = json.loads(self.arguments)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}


@dataclass
class FakeResponse:
    """A duck-typed model response readable the Anthropic way and the OpenAI way.

    ``.content`` is a list of blocks (``text`` / ``tool_use``) with ``.usage.input_tokens``;
    ``.choices[0].message.content`` and ``.choices[0].finish_reason`` follow the chat
    completions shape. ``.text`` is a shortcut for the reply text.
    """

    text: str = ""
    usage: Any = None
    model: str = "fake-model"
    stop_reason: str = "end_turn"
    finish_reason: str = "stop"
    tool_calls: tuple[ToolCall, ...] = ()
    refusal: str | None = None
    id: str = "fake-response"

    @property
    def content(self) -> list[Any]:
        blocks: list[Any] = []
        if self.text:
            blocks.append(SimpleNamespace(type="text", text=self.text))
        for call in self.tool_calls:
            blocks.append(
                SimpleNamespace(type="tool_use", id=call.id, name=call.name, input=call.input)
            )
        return blocks

    @property
    def choices(self) -> list[Any]:
        calls = [
            SimpleNamespace(
                id=c.id,
                type="function",
                function=SimpleNamespace(name=c.name, arguments=c.arguments),
            )
            for c in self.tool_calls
        ]
        message = SimpleNamespace(
            role="assistant",
            content=self.text or None,
            tool_calls=calls or None,
            refusal=self.refusal,
        )
        return [SimpleNamespace(index=0, message=message, finish_reason=self.finish_reason)]

    def __str__(self) -> str:
        return self.text


def _response(
    text: str,
    style: str,
    *,
    model: str = "fake-model",
    usage: tuple[int, int] | None = None,
    **extra: Any,
) -> FakeResponse:
    inp, out = usage if usage is not None else (0, _estimate(text))
    return FakeResponse(text=text, usage=make_usage(style, inp, out), model=model, **extra)


# ------------------------------------------------------------------------------ streams


@dataclass
class FakeChunk:
    """One streamed piece: ``.text``, ``.delta.text``, ``.choices[0].delta.content``."""

    text: str
    index: int = 0
    usage: Any = None
    finish_reason: str | None = None

    @property
    def delta(self) -> Any:
        return SimpleNamespace(text=self.text, content=self.text)

    @property
    def choices(self) -> list[Any]:
        return [SimpleNamespace(index=0, delta=self.delta, finish_reason=self.finish_reason)]

    def __str__(self) -> str:
        return self.text


class FakeStream:
    """A re-iterable scripted stream usable with ``for`` and ``async for``.

    Args:
        chunks: Strings or `FakeChunk` objects.
        first_token_delay: Extra seconds before the first chunk.
        chunk_delay: Seconds before each chunk: one number, or a sequence with one entry
            per chunk (a large entry is a stalled stream).
        error: Raise this (instance, class or factory) once ``error_after`` chunks have
            been delivered, after that gap's delay.
        error_after: Number of chunks delivered before ``error`` (default: all of them).
        usage: Usage attached to the last chunk.

    Delays use the package sleep, so ``fake_clock`` makes them instant.

    Example:
        ```python
        from python_wrap.llm_testing import FakeStream
        from python_wrap.testing import fake_clock

        stream = FakeStream(["a", "b"], chunk_delay=[0.5, 20.0])
        with fake_clock() as clock:
            assert [c.text for c in stream] == ["a", "b"]
        assert clock.total_slept == 20.5
        ```
    """

    def __init__(
        self,
        chunks: Sequence[str | FakeChunk],
        *,
        first_token_delay: float = 0.0,
        chunk_delay: float | Sequence[float] = 0.0,
        error: BaseException | type[BaseException] | Callable[[], BaseException] | None = None,
        error_after: int | None = None,
        usage: Any = None,
    ) -> None:
        self._chunks = [
            c if isinstance(c, FakeChunk) else FakeChunk(c, index=i) for i, c in enumerate(chunks)
        ]
        if first_token_delay < 0:
            raise ValueError("first_token_delay must be >= 0")
        if not isinstance(chunk_delay, (int, float)):
            chunk_delay = list(chunk_delay)
            if len(chunk_delay) != len(self._chunks):
                raise ValueError("chunk_delay sequence needs one entry per chunk")
        self._first = float(first_token_delay)
        self._delay = chunk_delay
        self._error = error
        self._error_after = len(self._chunks) if error_after is None else error_after
        if self._error_after < 0 or self._error_after > len(self._chunks):
            raise ValueError("error_after must be between 0 and the number of chunks")
        self.usage = usage
        if usage is not None and self._chunks:
            self._chunks[-1].usage = usage

    @property
    def text(self) -> str:
        """The full text of all chunks."""
        return "".join(c.text for c in self._chunks)

    def _gap(self, i: int) -> float:
        d = self._delay
        base = float(d) if isinstance(d, (int, float)) else float(d[i]) if i < len(d) else 0.0
        return base + (self._first if i == 0 else 0.0)

    def _raise(self) -> None:
        err = self._error
        if err is None:
            return
        if isinstance(err, BaseException):
            raise err
        made = err()
        raise made

    def __iter__(self) -> Iterator[FakeChunk]:
        for i, chunk in enumerate(self._chunks):
            gap = self._gap(i)
            if gap:
                _core.sleep(gap)
            if self._error is not None and i == self._error_after:
                self._raise()
            yield chunk
        if self._error is not None and self._error_after == len(self._chunks):
            self._raise()

    async def __aiter__(self) -> AsyncIterator[FakeChunk]:
        for i, chunk in enumerate(self._chunks):
            gap = self._gap(i)
            if gap:
                await _core.async_sleep(gap)
            if self._error is not None and i == self._error_after:
                self._raise()
            yield chunk
        if self._error is not None and self._error_after == len(self._chunks):
            self._raise()

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(
        self, typ: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None

    async def __aenter__(self) -> FakeStream:
        return self

    async def __aexit__(
        self, typ: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None


_WORDS = re.compile(r"\S+\s*")


def _split(text: str, size: int) -> list[str]:
    words = _WORDS.findall(text) or ([text] if text else [])
    return ["".join(words[i : i + size]) for i in range(0, len(words), size)]


# ------------------------------------------------------------------------------ provider


@dataclass
class FakeCall:
    """One recorded call. ``served`` is false when an injected fault answered instead."""

    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    stream: bool = False
    started: float = 0.0
    served: bool = False
    usage: tuple[int, int] = (0, 0)

    @property
    def model(self) -> Any:
        return self.kwargs.get("model")

    @property
    def messages(self) -> Any:
        return self.kwargs.get("messages")

    @property
    def prompt(self) -> str:
        """Text of the last message (or the first positional string), best effort."""
        messages = self.kwargs.get("messages")
        if isinstance(messages, Sequence) and not isinstance(messages, str) and messages:
            last = messages[-1]
            content = last.get("content") if isinstance(last, Mapping) else last
            return content if isinstance(content, str) else str(content)
        for value in (*self.args, self.kwargs.get("prompt")):
            if isinstance(value, str):
                return value
        return ""


Reply = Any  # str | BaseException | exception class | response object | callable(FakeCall)
UsageSpec = tuple[int, int] | Callable[[FakeCall, str], tuple[int, int]] | None


class _Endpoint:
    def __init__(self, fn: Callable[..., Any]) -> None:
        self.create = fn


class _Surface:
    """``messages.create`` / ``chat.completions.create`` look-alike namespaces."""

    def __init__(self, provider: FakeProvider, is_async: bool) -> None:
        async def acreate(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("stream"):
                return await provider.astream(*args, **kwargs)
            return await provider.acomplete(*args, **kwargs)

        def create(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("stream"):
                return provider.stream(*args, **kwargs)
            return provider.complete(*args, **kwargs)

        fn: Callable[..., Any] = acreate if is_async else create

        self.messages = _Endpoint(fn)
        self.chat = SimpleNamespace(completions=_Endpoint(fn))


class FakeProvider:
    """A deterministic, scripted model API for tests.

    Replies are served in order, one per call that reaches the provider. A reply can be a
    string (becomes a `FakeResponse`), an exception instance or class (raised), a
    `FakeResponse`/`FakeStream`/any other object or dict (returned as is) or a callable
    taking the `FakeCall` and returning one of those.

    Calls are recorded in `calls` even when a chaos rule answers instead of the provider.
    Fault rules for the provider's `point` (default ``"llm.<name>"``) apply to every entry
    point.

    Args:
        responses: The script.
        usage: ``(input_tokens, output_tokens)`` for every reply, a callable
            ``(call, text) -> (in, out)``, or ``None`` to estimate from text length.
        style: ``"anthropic"`` or ``"openai"`` usage objects.
        model: Model name reported on responses.
        name: Used to build the default point name.
        point: Fault point name.
        injector: The chaos injector to consult; defaults to the global `chaos`.
        cycle: Loop the script instead of raising `FakeProviderExhausted`.
        default: Reply used once the script runs out (takes precedence over ``cycle``).
        delay: Seconds each non-streaming call takes.
        first_token_delay: Seconds before a stream's first chunk.
        chunk_delay: Seconds before each stream chunk (for per-chunk gaps, make a
            `FakeStream` reply).
        chunk_size: Words per streamed chunk.

    Example:
        ```python
        from python_wrap.llm_testing import fake_llm

        llm = fake_llm(["hello"], usage=(10, 5))
        assert llm.complete(messages=[{"role": "user", "content": "hi"}]).text == "hello"
        llm.assert_called_with(messages=[{"role": "user", "content": "hi"}])
        llm.assert_tokens(input_tokens=10, output_tokens=5)
        ```
    """

    def __init__(
        self,
        responses: Sequence[Reply] = (),
        *,
        usage: UsageSpec = None,
        style: str = "anthropic",
        model: str = "fake-model",
        name: str = "fake",
        point: str | None = None,
        injector: FaultInjector | None = None,
        cycle: bool = False,
        default: Reply = None,
        delay: float = 0.0,
        first_token_delay: float = 0.0,
        chunk_delay: float = 0.0,
        chunk_size: int = 1,
    ) -> None:
        make_usage(style, 0, 0)  # validates the style
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        self._script = list(responses)
        self._usage = usage
        self.style = style
        self.model = model
        self.name = name
        self.point = point or f"llm.{name}"
        self._cycle = cycle
        self._default = default
        self._delay = delay
        self._first = first_token_delay
        self._chunk_delay = chunk_delay
        self._chunk_size = chunk_size
        self._lock = threading.Lock()
        self._next = 0
        self.calls: list[FakeCall] = []
        self._run = fault_point(self.point, injector=injector)(self._serve)
        self._arun = fault_point(self.point, injector=injector)(self._aserve)
        self.messages = _Surface(self, False).messages
        self.chat = _Surface(self, False).chat
        self.aio = _Surface(self, True)

    # -- entry points --------------------------------------------------------------

    def complete(self, *args: Any, **kwargs: Any) -> Any:
        """Serve the next reply (sync)."""
        return self._run(self._record(args, kwargs, False))

    async def acomplete(self, *args: Any, **kwargs: Any) -> Any:
        """Serve the next reply (async)."""
        return await self._arun(self._record(args, kwargs, False))

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        """Serve the next reply as a `FakeStream` (sync iteration or async)."""
        kwargs.pop("stream", None)
        return self._as_stream(self._run(self._record(args, kwargs, True)))

    async def astream(self, *args: Any, **kwargs: Any) -> Any:
        """Like `stream`, awaitable; iterate the result with ``async for``."""
        kwargs.pop("stream", None)
        return self._as_stream(await self._arun(self._record(args, kwargs, True)))

    __call__ = complete

    # -- internals -----------------------------------------------------------------

    def _record(self, args: tuple[Any, ...], kwargs: dict[str, Any], stream: bool) -> FakeCall:
        call = FakeCall(args, dict(kwargs), stream, _core.monotonic())
        with self._lock:
            self.calls.append(call)
        return call

    def _pick(self) -> Reply:
        with self._lock:
            if self._next < len(self._script):
                reply = self._script[self._next]
                self._next += 1
                return reply
            if self._default is not None:
                return self._default
            if self._cycle and self._script:
                reply = self._script[self._next % len(self._script)]
                self._next += 1
                return reply
            raise FakeProviderExhausted(
                f"{self.point}: script of {len(self._script)} replies is used up"
            )

    def _build(self, call: FakeCall) -> Any:
        call.served = True
        reply = self._pick()
        if callable(reply) and not isinstance(reply, type):
            reply = reply(call)
        if isinstance(reply, BaseException):
            raise reply
        if isinstance(reply, type) and issubclass(reply, BaseException):
            raise reply()
        if isinstance(reply, str):
            usage = self._usage
            if callable(usage):
                call.usage = usage(call, reply)
            elif usage is not None:
                call.usage = usage
            else:
                call.usage = (_estimate(call.prompt), _estimate(reply))
            return _response(reply, self.style, model=self.model, usage=call.usage)
        call.usage = _tokens(
            reply.get("usage") if isinstance(reply, Mapping) else getattr(reply, "usage", None)
        )
        return reply

    def _serve(self, call: FakeCall) -> Any:
        if self._delay and not call.stream:
            _core.sleep(self._delay)
        return self._build(call)

    async def _aserve(self, call: FakeCall) -> Any:
        if self._delay and not call.stream:
            await _core.async_sleep(self._delay)
        return self._build(call)

    def _as_stream(self, reply: Any) -> Any:
        if isinstance(reply, str):
            reply = FakeResponse(text=reply)
        if isinstance(reply, FakeResponse):
            chunks = _split(reply.text, self._chunk_size)
            return FakeStream(
                chunks,
                first_token_delay=self._first,
                chunk_delay=self._chunk_delay,
                usage=reply.usage,
            )
        return reply

    # -- assertions ----------------------------------------------------------------

    @property
    def call_count(self) -> int:
        """Calls made, including ones answered by an injected fault."""
        return len(self.calls)

    @property
    def served_count(self) -> int:
        """Calls that reached the provider (not intercepted by chaos)."""
        return sum(1 for c in self.calls if c.served)

    @property
    def input_tokens(self) -> int:
        """Total input tokens across served calls."""
        return sum(c.usage[0] for c in self.calls)

    @property
    def output_tokens(self) -> int:
        """Total output tokens across served calls."""
        return sum(c.usage[1] for c in self.calls)

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens."""
        return self.input_tokens + self.output_tokens

    def reset(self) -> None:
        """Forget recorded calls and rewind the script."""
        with self._lock:
            self.calls.clear()
            self._next = 0

    def assert_call_count(self, n: int) -> None:
        """Assert exactly ``n`` calls were made."""
        if len(self.calls) != n:
            raise AssertionError(f"expected {n} call(s) to {self.point}, got {len(self.calls)}")

    def assert_not_called(self) -> None:
        """Assert no call was made."""
        self.assert_call_count(0)

    def assert_called_with(self, *args: Any, **kwargs: Any) -> None:
        """Assert the most recent call included these arguments.

        Positional ``args`` must equal the start of the call's positional arguments, and
        every given keyword must be present with an equal value (other keywords such as
        ``max_tokens`` are ignored).

        Raises:
            AssertionError: If never called or the last call differs.
        """
        if not self.calls:
            raise AssertionError(f"expected a call to {self.point}, but it was never called")
        last = self.calls[-1]
        ok = last.args[: len(args)] == args and all(
            k in last.kwargs and last.kwargs[k] == v for k, v in kwargs.items()
        )
        if not ok:
            raise AssertionError(
                f"expected last call with args={args!r} kwargs={kwargs!r}, "
                f"got args={last.args!r} kwargs={last.kwargs!r}"
            )

    def assert_tokens(
        self, *, input_tokens: int | None = None, output_tokens: int | None = None
    ) -> None:
        """Assert the token totals (check only the ones you pass)."""
        if input_tokens is not None and self.input_tokens != input_tokens:
            raise AssertionError(f"expected {input_tokens} input tokens, got {self.input_tokens}")
        if output_tokens is not None and self.output_tokens != output_tokens:
            raise AssertionError(
                f"expected {output_tokens} output tokens, got {self.output_tokens}"
            )


def fake_llm(
    responses: Sequence[Reply] = (), *, usage: UsageSpec = None, **options: Any
) -> FakeProvider:
    """Build a `FakeProvider`; shorthand for ``FakeProvider(responses, usage=usage, ...)``."""
    return FakeProvider(responses, usage=usage, **options)


# ------------------------------------------------------------------------------- faults

_DEFAULT_JSON = '{"answer": "forty-two", "sources": ["alpha", "beta", "gamma"]}'


def _status_rule(
    cls: type[APIStatusError], status: int | None, retry_after: float | None, message: str
) -> dict[str, Any]:
    if retry_after is not None and (isinstance(retry_after, bool) or retry_after < 0):
        raise ValueError("retry_after must be a number >= 0")
    return {
        "exception": lambda: cls(message, status_code=status, retry_after=retry_after),
    }


def _b_rate_limit(
    *, retry_after: float | None = 1.0, message: str = "Rate limit exceeded"
) -> dict[str, Any]:
    return _status_rule(RateLimitError, 429, retry_after, message)


def _b_overloaded(*, message: str = "Overloaded") -> dict[str, Any]:
    return _status_rule(OverloadedError, 529, None, message)


def _b_server_error(*, status: int = 500, message: str = "Internal server error") -> dict[str, Any]:
    if not 500 <= status <= 599:
        raise ValueError("status must be a 5xx code")
    return _status_rule(InternalServerError, status, None, message)


def _b_timeout(*, after: float | None = None, message: str = "Request timed out") -> dict[str, Any]:
    rule: dict[str, Any] = {"exception": lambda: APITimeoutError(message)}
    if after is not None:
        rule["latency"] = after
    return rule


def _b_malformed_json(
    *, text: str = _DEFAULT_JSON, keep: float = 0.6, style: str = "anthropic"
) -> dict[str, Any]:
    if not 0 <= keep < 1:
        raise ValueError("keep must be in [0, 1)")
    cut = text[: int(len(text) * keep)]
    return {
        "result": _response(cut, style, stop_reason="max_tokens", finish_reason="length"),
    }


def _b_slow_first_token(*, delay: float = 5.0) -> dict[str, Any]:
    if delay < 0:
        raise ValueError("delay must be >= 0")
    return {"latency": float(delay)}


def _b_stalled_stream(
    *,
    after_chunks: int = 1,
    stall: float = 30.0,
    chunks: Sequence[str] = ("The", " answer", " is", " forty-two."),
    fail: bool = False,
    message: str = "Stream stalled",
) -> dict[str, Any]:
    items = list(chunks)
    if not 0 <= after_chunks < len(items) or stall < 0:
        raise ValueError("after_chunks must index a chunk and stall must be >= 0")
    delays = [0.0] * len(items)
    delays[after_chunks] = float(stall)
    stream = FakeStream(
        items,
        chunk_delay=delays,
        error=(lambda: APITimeoutError(message)) if fail else None,
        error_after=after_chunks if fail else None,
    )
    return {"result": stream}


def _b_invalid_tool_call(
    *,
    tool: str = "get_weather",
    arguments: str | Mapping[str, Any] = '{"city": ',
    style: str = "anthropic",
) -> dict[str, Any]:
    raw = arguments if isinstance(arguments, str) else json.dumps(dict(arguments))
    response = _response(
        "",
        style,
        stop_reason="tool_use",
        finish_reason="tool_calls",
        tool_calls=(ToolCall(tool, raw),),
    )
    return {"result": response}


def _b_refusal(
    *, message: str = "I can't help with that request.", style: str = "anthropic"
) -> dict[str, Any]:
    response = _response(
        message, style, stop_reason="refusal", finish_reason="content_filter", refusal=message
    )
    return {"result": response}


_BUILDERS: dict[str, Callable[..., dict[str, Any]]] = {
    "rate_limit": _b_rate_limit,
    "overloaded": _b_overloaded,
    "server_error": _b_server_error,
    "timeout": _b_timeout,
    "malformed_json": _b_malformed_json,
    "slow_first_token": _b_slow_first_token,
    "stalled_stream": _b_stalled_stream,
    "invalid_tool_call": _b_invalid_tool_call,
    "refusal": _b_refusal,
}


def build_rule(kind: str, options: Mapping[str, Any]) -> dict[str, Any]:
    """Translate an LLM fault ``kind`` and its options into `FaultInjector.inject` kwargs.

    Raises:
        ValueError: For an unknown kind or option, or an invalid value.
    """
    builder = _BUILDERS.get(kind)
    if builder is None:
        raise ValueError(f"unknown llm fault {kind!r} (use one of {', '.join(sorted(_BUILDERS))})")
    try:
        return builder(**options)
    except TypeError as exc:
        raise ValueError(f"llm fault {kind!r}: {exc}") from exc


class LLMFaults:
    """LLM-shaped fault rules on a `FaultInjector`, reached as ``chaos.llm``.

    Every method adds one rule for the point name or glob ``name`` and returns the
    `Fault` handle (a context manager). ``rate``, ``times`` and seeding behave as for
    `FaultInjector.inject`. Provider replies that carry data (truncated JSON, bad tool
    call, refusal) replace the call's result, so they exercise your parsing, not your
    transport.

    Example:
        ```python
        from python_wrap import retry
        from python_wrap.chaos import chaos
        from python_wrap.llm_testing import RateLimitError, fake_llm
        from python_wrap.testing import fake_clock

        llm = fake_llm(["done"])

        @retry(max_attempts=3, delay=1.0, on=RateLimitError)
        def ask() -> str:
            return llm.complete(prompt="hi").text

        with fake_clock(), chaos.llm.rate_limit("llm.*", times=2, retry_after=1):
            assert ask() == "done"
        assert llm.call_count == 3
        ```
    """

    def __init__(self, injector: FaultInjector) -> None:
        self._injector = injector

    def _add(self, name: str, rate: float, times: int | None, kind: str, **options: Any) -> Fault:
        return self._injector.inject(name, rate=rate, times=times, **build_rule(kind, options))

    def rate_limit(
        self,
        name: str,
        *,
        retry_after: float | None = 1.0,
        rate: float = 1.0,
        times: int | None = None,
        message: str = "Rate limit exceeded",
    ) -> Fault:
        """Raise `RateLimitError` (429) carrying a ``Retry-After`` of ``retry_after`` seconds."""
        return self._add(name, rate, times, "rate_limit", retry_after=retry_after, message=message)

    def overloaded(
        self, name: str, *, rate: float = 1.0, times: int | None = None, message: str = "Overloaded"
    ) -> Fault:
        """Raise `OverloadedError` (529)."""
        return self._add(name, rate, times, "overloaded", message=message)

    def server_error(
        self,
        name: str,
        *,
        status: int = 500,
        rate: float = 1.0,
        times: int | None = None,
        message: str = "Internal server error",
    ) -> Fault:
        """Raise `InternalServerError` with a 5xx ``status``."""
        return self._add(name, rate, times, "server_error", status=status, message=message)

    def timeout(
        self,
        name: str,
        *,
        after: float | None = None,
        rate: float = 1.0,
        times: int | None = None,
        message: str = "Request timed out",
    ) -> Fault:
        """Raise `APITimeoutError`, optionally after waiting ``after`` seconds."""
        return self._add(name, rate, times, "timeout", after=after, message=message)

    def malformed_json(
        self,
        name: str,
        *,
        text: str = _DEFAULT_JSON,
        keep: float = 0.6,
        style: str = "anthropic",
        rate: float = 1.0,
        times: int | None = None,
    ) -> Fault:
        """Return a response whose text is ``text`` cut to the first ``keep`` fraction."""
        return self._add(name, rate, times, "malformed_json", text=text, keep=keep, style=style)

    def slow_first_token(
        self, name: str, *, delay: float = 5.0, rate: float = 1.0, times: int | None = None
    ) -> Fault:
        """Wait ``delay`` seconds before the call (a slow time-to-first-token)."""
        return self._add(name, rate, times, "slow_first_token", delay=delay)

    def stalled_stream(
        self,
        name: str,
        *,
        after_chunks: int = 1,
        stall: float = 30.0,
        chunks: Sequence[str] = ("The", " answer", " is", " forty-two."),
        fail: bool = False,
        rate: float = 1.0,
        times: int | None = None,
    ) -> Fault:
        """Answer with a stream that pauses ``stall`` seconds after ``after_chunks`` chunks.

        With ``fail=True`` the stream raises `APITimeoutError` after the pause instead of
        continuing.
        """
        return self._add(
            name,
            rate,
            times,
            "stalled_stream",
            after_chunks=after_chunks,
            stall=stall,
            chunks=chunks,
            fail=fail,
        )

    def invalid_tool_call(
        self,
        name: str,
        *,
        tool: str = "get_weather",
        arguments: str | Mapping[str, Any] = '{"city": ',
        style: str = "anthropic",
        rate: float = 1.0,
        times: int | None = None,
    ) -> Fault:
        """Return a response asking for ``tool`` with broken (or wrongly typed) arguments."""
        return self._add(
            name, rate, times, "invalid_tool_call", tool=tool, arguments=arguments, style=style
        )

    def refusal(
        self,
        name: str,
        *,
        message: str = "I can't help with that request.",
        style: str = "anthropic",
        rate: float = 1.0,
        times: int | None = None,
    ) -> Fault:
        """Return a content-policy refusal (``stop_reason="refusal"``, ``content_filter``)."""
        return self._add(name, rate, times, "refusal", message=message, style=style)
