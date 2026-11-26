"""Guard a LangChain chat model: one retry owner, one budget, one token limit, safe streams.

LangChain models retry by themselves (``max_retries``, ``with_retry``) on top of the
provider SDK, so a python-wrap retry around them multiplies attempts and ignores the
`RetryBudget`. `guard_chat_model` rebuilds the model with ``max_retries=0`` and runs
``invoke``/``ainvoke`` through the same layers as `guard_openai`: tracing (optional), the
`Budget` (charged once with the final ``usage_metadata``), python-wrap's retry loop, your
per-attempt ``policy`` and the `TokenRateLimiter` (lowered to the provider's
rate-limit headers when ``response_metadata["headers"]`` carries them).
``stream``/``astream`` return a `ResumableStream`: reopened only before any output.

`PythonWrapCallback` only observes (usage, latency, errors) and emits the
``genai.llm_call`` event, for code paths where the model cannot be wrapped.

LangChain is never imported here.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from python_wrap import _core
from python_wrap.adapters._sdk import GuardedClient, ResumableStream
from python_wrap.llm import Budget, TokenRateLimiter, read_usage

__all__ = ["GuardedChatModel", "PythonWrapCallback", "guard_chat_model", "without_retries"]


def without_retries(model: Any) -> Any:
    """A copy of a LangChain chat model (or a binding around one) that never retries.

    The model is rebuilt from the fields you set plus ``max_retries=0``, so the
    provider client inside it is created again without its own retries. A
    ``with_retry()`` wrapper is refused: unwrap it and let python-wrap retry.

    Raises:
        ValueError: For a ``RunnableRetry`` (``model.with_retry(...)``).
        TypeError: If a retrying model cannot be rebuilt.
    """
    if (getattr(model, "max_attempt_number", 0) or 0) > 1:
        raise ValueError("unwrap .with_retry(): python-wrap owns the retries")
    bound = getattr(model, "bound", None)
    if bound is not None and hasattr(model, "model_copy"):  # bind_tools() and friends
        return model.model_copy(update={"bound": without_retries(bound)})
    if not getattr(model, "max_retries", 0):
        return model
    fields = {name: getattr(model, name) for name in getattr(model, "model_fields_set", ())}
    try:
        return type(model)(**{**fields, "max_retries": 0})
    except Exception as exc:
        raise TypeError(
            f"cannot rebuild {type(model).__name__} with max_retries=0; "
            "construct it with max_retries=0 yourself"
        ) from exc


class _NoSdk:
    """Stands in for an SDK client: the model's own retries were removed already."""

    max_retries = 0


def _text(value: Any) -> str:
    """The prompt text of a LangChain input, for the token estimate."""
    if isinstance(value, str):
        return value
    to_string = getattr(value, "to_string", None)  # a PromptValue
    if callable(to_string):
        return str(to_string())
    if isinstance(value, (list, tuple)):
        return "\n".join(_text(getattr(item, "content", item)) for item in value)
    if isinstance(value, Mapping):
        return _text(value.get("content", ""))
    return str(value)


def _headers(result: Any) -> Any:
    meta = getattr(result, "response_metadata", None)
    return meta.get("headers") if isinstance(meta, Mapping) else None


class GuardedChatModel:
    """The wrapper `guard_chat_model` returns.

    ``invoke``, ``ainvoke``, ``stream`` and ``astream`` are guarded; every other attribute
    is the retry-free model's (``batch`` and tools such as ``bind_tools`` are not guarded).

    Attributes:
        raw: The model you passed.
        model: The copy calls go through (``max_retries=0``).
    """

    def __init__(self, model: Any, **options: Any) -> None:
        self.raw = model
        self.model = without_retries(model)
        self._sync = GuardedClient(_NoSdk(), system="langchain", is_async=False, **options)
        self._async = GuardedClient(_NoSdk(), system="langchain", is_async=True, **options)
        self._invoke = self._sync._compose("langchain.invoke", self._attempt)
        self._ainvoke = self._async._compose("langchain.ainvoke", self._aattempt)

    def _attempt(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        request = {"input": _text(input)}
        cost = self._sync._reserve((), request, "langchain.invoke")
        try:
            result = self.model.invoke(input, config, **kwargs)
        except BaseException as exc:
            self._sync._failed(cost, exc, request)
            raise
        self._sync._settled(cost, result, _headers(result), request)
        return result

    async def _aattempt(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        request = {"input": _text(input)}
        cost = await self._async._areserve((), request, "langchain.ainvoke")
        try:
            result = await self.model.ainvoke(input, config, **kwargs)
        except BaseException as exc:
            self._async._failed(cost, exc, request)
            raise
        self._async._settled(cost, result, _headers(result), request)
        return result

    def invoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        return self._invoke(input, config, **kwargs)

    async def ainvoke(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        return await self._ainvoke(input, config, **kwargs)

    def stream(self, input: Any, config: Any = None, **kwargs: Any) -> ResumableStream[Any]:
        """Chunks of the model's stream; reopened on a transient error before output."""
        request = {"input": _text(input)}
        admission = self._sync._admit((input,), request, "langchain.stream")
        return self._sync._resumable(
            "langchain.stream",
            lambda: self.model.stream(input, config, **kwargs),
            admission,
            request,
        )

    async def astream(self, input: Any, config: Any = None, **kwargs: Any) -> Any:
        """Async `stream`: iterate the result with ``async for``."""
        request = {"input": _text(input)}
        admission = await self._async._aadmit((input,), request, "langchain.astream")
        stream = self._async._resumable(
            "langchain.astream",
            lambda: self.model.astream(input, config, **kwargs),
            admission,
            request,
        )
        async for chunk in stream:
            yield chunk

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.model, name)

    def __repr__(self) -> str:
        return f"GuardedChatModel({type(self.raw).__name__}, attempts={self._sync.attempts})"


def guard_chat_model(
    model: Any,
    *,
    budget: Budget | None = None,
    limiter: TokenRateLimiter | None = None,
    policy: Callable[[Callable[..., Any]], Callable[..., Any]] | None = None,
    attempts: int = 4,
    retry: Mapping[str, Any] | None = None,
    trace: bool | Mapping[str, Any] = False,
    first_token: float | None = None,
    chunk_gap: float | None = None,
    total: float | None = None,
) -> GuardedChatModel:
    """Wrap a LangChain chat model so each call is retried, budgeted and limited once.

    Args:
        model: A LangChain chat model (``ChatOpenAI``, ``ChatAnthropic``...) or a binding
            around one (``model.bind_tools(...)``).
        budget: Checked before each call, charged once with the final ``usage_metadata``.
        limiter: Reserves an estimate per attempt and settles with the real usage.
        policy: A per-attempt decorator (a timeout, a breaker); no retry in it.
        attempts: Total attempts per call (python-wrap's `llm_retry`).
        retry: Extra `llm_retry` options (``delay``, ``max_delay``, ``retry_if``...).
        trace: Wrap calls in `traced_llm` (True, or its options).
        first_token: Seconds to wait for a stream's first chunk.
        chunk_gap: Longest pause between stream chunks.
        total: Longest whole stream.

    Returns:
        A `GuardedChatModel`.

    Example:
        ```python
        from python_wrap.adapters.langchain import guard_chat_model


        class FakeModel:  # stands in for ChatOpenAI(...)
            max_retries = 0

            def invoke(self, input, config=None):
                return {"content": "hi", "usage": {"input_tokens": 3, "output_tokens": 1}}


        assert guard_chat_model(FakeModel()).invoke("hello")["content"] == "hi"
        ```
    """
    return GuardedChatModel(
        model,
        budget=budget,
        limiter=limiter,
        policy=policy,
        attempts=attempts,
        retry=retry,
        trace=trace,
        first_token=first_token,
        chunk_gap=chunk_gap,
        total=total,
    )


class PythonWrapCallback:
    """A LangChain callback handler that observes calls and emits ``genai.llm_call``.

    Use it where a model cannot be wrapped (inside a chain built elsewhere):
    ``model.invoke(prompt, config={"callbacks": [PythonWrapCallback()]})``. It records
    tokens, latency, model and errors; it never changes a call and captures no content.
    Duck-typed: LangChain need not be importable when it is created.
    """

    # Attributes LangChain's callback manager reads on every handler.
    ignore_llm = ignore_chat_model = ignore_retry = False
    ignore_chain = ignore_agent = ignore_retriever = ignore_custom_event = True
    raise_error = False
    run_inline = True

    def __init__(self, name: str = "langchain") -> None:
        self.name = name
        self._started: dict[Any, tuple[float, float]] = {}

    def on_llm_start(self, serialized: Any, prompts: Any, *, run_id: Any = None, **_: Any) -> None:
        self._started[run_id] = (time.monotonic(), time.time())

    def on_chat_model_start(
        self, serialized: Any, messages: Any, *, run_id: Any = None, **_: Any
    ) -> None:
        self._started[run_id] = (time.monotonic(), time.time())

    def _emit(self, run_id: Any, status: str, **fields: Any) -> None:
        began, wall = self._started.pop(run_id, (time.monotonic(), time.time()))
        _core.emit(
            "genai.llm_call",
            self.name,
            kind="llm",
            run_id=str(run_id or uuid.uuid4()),
            step=None,
            tool=None,
            system="langchain",
            cost=None,
            latency=time.monotonic() - began,
            started=wall,
            status=status,
            attempts=None,
            **{"model": None, "input_tokens": None, "output_tokens": None, "error_type": None}
            | fields,
        )

    def on_llm_end(self, response: Any, *, run_id: Any = None, **_: Any) -> None:
        output = getattr(response, "llm_output", None) or {}
        usage = read_usage({"usage": output.get("token_usage") or output.get("usage")})
        if usage is None:  # chat models put usage_metadata on the message
            for batch in getattr(response, "generations", None) or ():
                for generation in batch:
                    usage = read_usage(getattr(generation, "message", None)) or usage
        model = output.get("model_name") or (usage.model if usage else None)
        self._emit(
            run_id,
            "ok",
            model=model,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
        )

    def on_llm_error(self, error: BaseException, *, run_id: Any = None, **_: Any) -> None:
        self._emit(run_id, "error", error_type=type(error).__name__)
