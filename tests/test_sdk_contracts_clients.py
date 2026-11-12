"""Contract tests: the adapters against the REAL ``openai`` / ``anthropic`` packages.

Each SDK talks to an in-process mock transport (``httpx2``/``httpx`` ``MockTransport``),
so nothing leaves the machine and no key is needed. The tests skip cleanly when an SDK
is not installed: python-wrap itself never depends on them. Run them with
``pytest -m sdk`` in an environment where ``openai`` and ``anthropic`` are installed.
"""

from __future__ import annotations

import importlib
import json
import warnings
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import pytest

from python_wrap.adapters import (
    CacheGuard,
    PromptCacheWarning,
    StreamInterrupted,
    guard_anthropic,
    guard_openai,
    no_sdk_retries,
    sdk_max_retries,
)
from python_wrap.doctor import doctor
from python_wrap.llm import Budget, llm_retry, token_rate_limit

pytestmark = pytest.mark.sdk

MODEL = "model-test"


def _http() -> Any:
    """The HTTP library the installed SDKs are built on (``httpx2`` or ``httpx``)."""
    for name in ("httpx2", "httpx"):
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    pytest.skip("no httpx/httpx2 installed")


Handler = Callable[[Any], Any]


class Recorder:
    """A MockTransport handler serving scripted replies and recording requests."""

    def __init__(self, *replies: Callable[[Any], Any]) -> None:
        self.replies = list(replies)
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        index = min(len(self.requests), len(self.replies)) - 1
        return self.replies[index](request)

    def body(self, index: int = -1) -> dict[str, Any]:
        return dict(json.loads(self.requests[index].content))


def _json(status: int, payload: Any, headers: dict[str, str] | None = None) -> Handler:
    def reply(request: Any) -> Any:
        return _http().Response(status, json=payload, headers=headers or {})

    return reply


def _sse(events: list[tuple[str | None, Any]], *, fail_after: int | None = None) -> Handler:
    """A ``text/event-stream`` reply; ``fail_after`` events, then a transport error."""
    http = _http()
    chunks = []
    for name, data in events:
        line = data if isinstance(data, str) else json.dumps(data)
        chunks.append(((f"event: {name}\n" if name else "") + f"data: {line}\n\n").encode())

    class Body(http.SyncByteStream, http.AsyncByteStream):  # type: ignore[misc,name-defined]
        def __iter__(self) -> Iterator[bytes]:
            for index, chunk in enumerate(chunks):
                if fail_after is not None and index == fail_after:
                    raise http.ReadError("connection reset")
                yield chunk
            if fail_after is not None and fail_after >= len(chunks):
                raise http.ReadError("connection reset")

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for chunk in self.__iter__():
                yield chunk

    def reply(request: Any) -> Any:
        return http.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())

    return reply


def _sync_client(module: Any, cls: str, recorder: Recorder, **options: Any) -> Any:
    http = _http()
    transport = http.MockTransport(recorder)
    base = "http://sdk.test/v1" if module.__name__ == "openai" else "http://sdk.test"
    return getattr(module, cls)(
        api_key="test-key",
        base_url=base,
        http_client=http.Client(transport=transport),
        **options,
    )


def _async_client(module: Any, cls: str, recorder: Recorder, **options: Any) -> Any:
    http = _http()
    transport = http.MockTransport(recorder)
    base = "http://sdk.test/v1" if module.__name__ == "openai" else "http://sdk.test"
    return getattr(module, cls)(
        api_key="test-key",
        base_url=base,
        http_client=http.AsyncClient(transport=transport),
        **options,
    )


# ------------------------------------------------------------------------- OpenAI


@pytest.fixture
def openai() -> Any:
    return pytest.importorskip("openai")


def _completion(content: str = "hi", prompt: int = 10, completion: int = 2) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }


def _chunk(delta: dict[str, Any], finish: str | None = None, usage: Any = None) -> dict[str, Any]:
    choices = [] if delta is None else [{"index": 0, "delta": delta, "finish_reason": finish}]
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": MODEL,
        "choices": choices,
        "usage": usage,
    }


CHAT_TOOL_STREAM: list[tuple[str | None, Any]] = [
    (None, _chunk({"role": "assistant", "content": ""})),
    (None, _chunk({"content": "Checking "})),
    (
        None,
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "order_status", "arguments": '{"order'},
                    }
                ]
            }
        ),
    ),
    (None, _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '_id": "A-1"}'}}]})),
    (None, _chunk({}, finish="tool_calls")),
    (
        None,
        {**_chunk({}), "choices": [], "usage": {"prompt_tokens": 30, "completion_tokens": 9}},
    ),
    (None, "[DONE]"),
]

LIMIT_HEADERS = {"x-ratelimit-remaining-tokens": "500", "x-ratelimit-limit-tokens": "100000"}


def test_openai_no_sdk_retries_copies_the_client(openai: Any) -> None:
    client = _sync_client(openai, "OpenAI", Recorder(_json(200, _completion())))
    quiet = no_sdk_retries(client)
    assert sdk_max_retries(client) == 2
    assert sdk_max_retries(quiet) == 0
    assert type(quiet) is type(client)


def test_openai_layered_retries_multiply_and_no_sdk_retries_fixes_it(openai: Any) -> None:
    fail = _json(
        500, {"error": {"message": "boom", "type": "server_error"}}, {"retry-after-ms": "1"}
    )
    recorder = Recorder(fail)
    client = _sync_client(openai, "OpenAI", recorder)

    @llm_retry(max_attempts=2, delay=0, jitter=0, client=client)
    def naive() -> Any:
        return client.chat.completions.create(model=MODEL, messages=[])

    with pytest.raises(openai.InternalServerError):
        naive()
    assert len(recorder.requests) == 2 * 3  # python-wrap 2 x SDK (1 + 2 retries)
    assert "PW018" in [f.code for f in doctor(naive).findings]

    recorder.requests.clear()
    quiet = no_sdk_retries(client)

    @llm_retry(max_attempts=2, delay=0, jitter=0, client=quiet)
    def bounded() -> Any:
        return quiet.chat.completions.create(model=MODEL, messages=[])

    with pytest.raises(openai.InternalServerError):
        bounded()
    assert len(recorder.requests) == 2
    assert "PW018" not in [f.code for f in doctor(bounded).findings]


def test_openai_guard_retries_once_per_layer_and_charges_usage(openai: Any) -> None:
    limited = _json(
        429,
        {"error": {"message": "slow down", "type": "rate_limit_error"}},
        {"retry-after-ms": "1", "x-ratelimit-remaining-tokens": "0"},
    )
    recorder = Recorder(limited, _json(200, _completion(prompt=40, completion=5), LIMIT_HEADERS))
    spend = Budget(tokens=10_000)
    limiter = token_rate_limit(100_000)
    guarded = guard_openai(
        _sync_client(openai, "OpenAI", recorder),
        budget=spend,
        limiter=limiter,
        retry={"delay": 0, "jitter": 0},
    )
    result = guarded.chat.completions.create(
        model=MODEL, messages=[{"role": "user", "content": "hi"}]
    )
    assert result.choices[0].message.content == "hi"
    assert len(recorder.requests) == 2  # one 429, one success: the SDK did not retry too
    assert spend.spent().tokens == 45
    assert limiter.available <= 500 + 1  # lowered to x-ratelimit-remaining-tokens
    assert guarded.client.max_retries == 0 and guarded.raw.max_retries == 2


def test_openai_guard_bad_request_is_not_retried(openai: Any) -> None:
    recorder = Recorder(_json(400, {"error": {"message": "bad", "type": "invalid_request_error"}}))
    guarded = guard_openai(_sync_client(openai, "OpenAI", recorder), retry={"delay": 0})
    with pytest.raises(openai.BadRequestError):
        guarded.chat.completions.create(model=MODEL, messages=[])
    assert len(recorder.requests) == 1


def test_openai_responses_create_reads_usage(openai: Any) -> None:
    payload = {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": MODEL,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello", "annotations": []}],
            }
        ],
        "usage": {
            "input_tokens": 2000,
            "output_tokens": 7,
            "total_tokens": 2007,
            "input_tokens_details": {"cached_tokens": 1536},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }
    spend = Budget(tokens=100_000)
    cache = CacheGuard(min_calls=1, min_prompt_tokens=1024)
    guarded = guard_openai(
        _sync_client(openai, "OpenAI", Recorder(_json(200, payload))),
        budget=spend,
        cache_guard=cache,
    )
    response = guarded.responses.create(model=MODEL, input="hi")
    assert response.output_text == "hello"
    assert spend.spent().tokens == 2007
    assert cache.stats().cache_read_tokens == 1536


def test_openai_chat_stream_tracks_text_tools_and_usage(openai: Any) -> None:
    recorder = Recorder(_sse(CHAT_TOOL_STREAM))
    spend = Budget(tokens=10_000)
    guarded = guard_openai(_sync_client(openai, "OpenAI", recorder), budget=spend)
    stream = guarded.chat.completions.create(model=MODEL, messages=[], stream=True)
    chunks = list(stream)
    assert len(chunks) == 6
    assert stream.text == "Checking "
    [call] = stream.tool_calls
    assert (call.id, call.name, call.input) == ("call_1", "order_status", {"order_id": "A-1"})
    assert spend.spent().tokens == 39
    assert recorder.body()["stream_options"] == {"include_usage": True}


def test_openai_stream_failing_before_output_is_reopened(openai: Any) -> None:
    unavailable = _json(503, {"error": {"message": "busy", "type": "server_error"}})
    recorder = Recorder(unavailable, _sse(CHAT_TOOL_STREAM, fail_after=1), _sse(CHAT_TOOL_STREAM))
    guarded = guard_openai(_sync_client(openai, "OpenAI", recorder), retry={"delay": 0})
    stream = guarded.chat.completions.create(model=MODEL, messages=[], stream=True)
    chunks = list(stream)
    assert stream.attempts == 3  # 503 on open, reset after the role-only chunk, success
    assert len(chunks) == 6  # the role-only chunk of the failed attempt is not repeated
    assert len(stream.tool_calls) == 1


def test_openai_stream_failing_mid_tool_call_is_not_replayed(openai: Any) -> None:
    recorder = Recorder(_sse(CHAT_TOOL_STREAM, fail_after=3), _sse(CHAT_TOOL_STREAM))
    guarded = guard_openai(_sync_client(openai, "OpenAI", recorder), retry={"delay": 0})
    stream = guarded.chat.completions.create(model=MODEL, messages=[], stream=True)
    with pytest.raises(StreamInterrupted) as caught:
        list(stream)
    assert caught.value.text == "Checking "
    assert caught.value.tool_calls == ()
    assert [p.name for p in caught.value.pending] == ["order_status"]
    assert len(recorder.requests) == 1


async def test_openai_async_client_create_and_stream(openai: Any) -> None:
    recorder = Recorder(_json(500, {"error": {"type": "server_error"}}), _json(200, _completion()))
    spend = Budget(tokens=10_000)
    guarded = guard_openai(
        _async_client(openai, "AsyncOpenAI", recorder), budget=spend, retry={"delay": 0}
    )
    assert guarded.is_async
    result = await guarded.chat.completions.create(model=MODEL, messages=[])
    assert result.choices[0].message.content == "hi"
    assert len(recorder.requests) == 2

    recorder.replies = [_sse(CHAT_TOOL_STREAM)]
    recorder.requests.clear()
    stream = await guarded.chat.completions.create(model=MODEL, messages=[], stream=True)
    chunks = [chunk async for chunk in stream]
    assert len(chunks) == 6 and stream.tool_calls[0].name == "order_status"
    assert spend.spent().tokens == 12 + 39


# ---------------------------------------------------------------------- Anthropic


@pytest.fixture
def anthropic() -> Any:
    return pytest.importorskip("anthropic")


def _message(text: str = "hi", **usage: int) -> dict[str, Any]:
    counts = {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        **usage,
    }
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": counts,
    }


def _tool_message() -> dict[str, Any]:
    message = _message()
    message["content"] = [
        {"type": "tool_use", "id": "toolu_1", "name": "order_status", "input": {"order_id": "A-1"}}
    ]
    message["stop_reason"] = "tool_use"
    return message


MESSAGE_STREAM: list[tuple[str | None, Any]] = [
    (
        "message_start",
        {
            "type": "message_start",
            "message": {
                **_message(""),
                "content": [],
                "usage": {
                    "input_tokens": 25,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
    ),
    ("ping", {"type": "ping"}),
    (
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "On it."},
        },
    ),
    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
    (
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "order_status",
                "input": {},
            },
        },
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"order_id": '},
        },
    ),
    (
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '"A-1"}'},
        },
    ),
    ("content_block_stop", {"type": "content_block_stop", "index": 1}),
    (
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use", "stop_sequence": None},
            "usage": {"output_tokens": 14},
        },
    ),
    ("message_stop", {"type": "message_stop"}),
]


def test_anthropic_no_sdk_retries_copies_the_client(anthropic: Any) -> None:
    client = _sync_client(anthropic, "Anthropic", Recorder(_json(200, _message())))
    assert sdk_max_retries(client) == 2
    assert sdk_max_retries(no_sdk_retries(client)) == 0


def test_anthropic_guard_retries_overloaded_once_and_seeds_limiter(anthropic: Any) -> None:
    overloaded = _json(
        529,
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        {"retry-after": "0"},
    )
    headers = {"anthropic-ratelimit-tokens-remaining": "800"}
    recorder = Recorder(overloaded, _json(200, _message(input_tokens=30, output_tokens=4), headers))
    spend = Budget(tokens=10_000)
    limiter = token_rate_limit(50_000)
    guarded = guard_anthropic(
        _sync_client(anthropic, "Anthropic", recorder),
        budget=spend,
        limiter=limiter,
        retry={"delay": 0, "jitter": 0},
    )
    message = guarded.messages.create(model=MODEL, max_tokens=50, messages=[])
    assert message.content[0].text == "hi"
    assert len(recorder.requests) == 2
    assert spend.spent().tokens == 34
    assert limiter.available <= 800 + 1


def test_anthropic_create_stream_and_stream_manager(anthropic: Any) -> None:
    recorder = Recorder(_sse(MESSAGE_STREAM))
    spend = Budget(tokens=10_000)
    guarded = guard_anthropic(_sync_client(anthropic, "Anthropic", recorder), budget=spend)
    stream = guarded.messages.create(model=MODEL, max_tokens=50, messages=[], stream=True)
    events = list(stream)
    assert [e.type for e in events][:2] == ["message_start", "content_block_start"]
    assert stream.text == "On it."
    assert [(c.id, c.input) for c in stream.tool_calls] == [("toolu_1", {"order_id": "A-1"})]
    assert spend.spent().tokens == 25 + 14

    recorder.requests.clear()
    with guarded.messages.stream(model=MODEL, max_tokens=50, messages=[]) as managed:
        texts = list(managed.text_stream)
        final = managed.get_final_message()
    assert texts == ["On it."]  # the SDK's derived "text" events are not double counted
    assert final.stop_reason == "tool_use"
    assert spend.spent().tokens == 2 * 39


def test_anthropic_stream_failing_after_text_raises_interrupted(anthropic: Any) -> None:
    recorder = Recorder(_sse(MESSAGE_STREAM, fail_after=7))
    guarded = guard_anthropic(_sync_client(anthropic, "Anthropic", recorder))
    stream = guarded.messages.create(model=MODEL, max_tokens=50, messages=[], stream=True)
    with pytest.raises(StreamInterrupted) as caught:
        list(stream)
    assert caught.value.text == "On it."
    assert caught.value.pending[0].arguments == '{"order_id": '
    assert len(recorder.requests) == 1


def test_anthropic_stream_failing_after_message_start_is_reopened(anthropic: Any) -> None:
    recorder = Recorder(_sse(MESSAGE_STREAM, fail_after=2), _sse(MESSAGE_STREAM))
    guarded = guard_anthropic(_sync_client(anthropic, "Anthropic", recorder), retry={"delay": 0})
    stream = guarded.messages.create(model=MODEL, max_tokens=50, messages=[], stream=True)
    events = list(stream)
    assert stream.attempts == 2
    assert [e.type for e in events].count("message_start") == 1


def test_anthropic_overloaded_error_event_inside_the_stream_is_reopened(anthropic: Any) -> None:
    failing = [
        MESSAGE_STREAM[0],
        (
            "error",
            {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        ),
    ]
    recorder = Recorder(_sse(failing), _sse(MESSAGE_STREAM))
    guarded = guard_anthropic(_sync_client(anthropic, "Anthropic", recorder), retry={"delay": 0})
    stream = guarded.messages.create(model=MODEL, max_tokens=50, messages=[], stream=True)
    list(stream)
    assert stream.attempts == 2 and stream.text == "On it."


def test_anthropic_tool_runner_steps_are_budgeted(anthropic: Any) -> None:
    recorder = Recorder(_json(200, _tool_message()), _json(200, _message("A-1 has shipped.")))
    spend = Budget(tokens=10_000)

    @anthropic.beta_tool
    def order_status(order_id: str) -> str:
        """Look up an order."""
        return "shipped"

    guarded = guard_anthropic(_sync_client(anthropic, "Anthropic", recorder), budget=spend)
    runner = guarded.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=50,
        tools=[order_status],
        messages=[{"role": "user", "content": "A-1?"}],
    )
    final = runner.until_done()
    assert final.content[0].text == "A-1 has shipped."
    assert runner.steps == 2
    assert spend.spent().tokens == 24


def test_anthropic_cache_guard_warns_without_cache_control(anthropic: Any) -> None:
    recorder = Recorder(_json(200, _message(input_tokens=3000)))
    cache = CacheGuard(min_calls=2)
    guarded = guard_anthropic(_sync_client(anthropic, "Anthropic", recorder), cache_guard=cache)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(2):
            guarded.messages.create(model=MODEL, max_tokens=5, system="Be brief.", messages=[])
    [warning] = [w for w in caught if w.category is PromptCacheWarning]
    assert "cache_control" in str(warning.message)


async def test_anthropic_async_client(anthropic: Any) -> None:
    recorder = Recorder(
        _json(529, {"type": "error", "error": {"type": "overloaded_error"}}), _json(200, _message())
    )
    spend = Budget(tokens=1_000)
    guarded = guard_anthropic(
        _async_client(anthropic, "AsyncAnthropic", recorder), budget=spend, retry={"delay": 0}
    )
    message = await guarded.messages.create(model=MODEL, max_tokens=5, messages=[])
    assert message.content[0].text == "hi" and len(recorder.requests) == 2

    recorder.replies = [_sse(MESSAGE_STREAM)]
    async with guarded.messages.stream(model=MODEL, max_tokens=50, messages=[]) as stream:
        texts = [text async for text in stream.text_stream]
    assert texts == ["On it."]
    assert spend.spent().tokens == 12 + 39
