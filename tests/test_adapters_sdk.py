"""The OpenAI / Anthropic SDK adapters, against hand-built fakes shaped like the SDKs.

The fakes mirror what the real packages expose: clients with ``max_retries`` and
``with_options``, resources with ``create`` and ``with_raw_response.create`` (whose
result has ``headers`` and ``parse()``), usage objects with the real field names, stream
event objects with the real ``type`` values, and errors carrying ``status_code``,
``response.headers`` and ``body``. ``tests/test_sdk_contracts.py`` runs the same paths
against the real SDKs when they are installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import threading
import warnings
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from python_wrap.adapters import (
    CacheGuard,
    GuardedAnthropic,
    GuardedOpenAI,
    PromptCacheWarning,
    ResumableStream,
    StreamInterrupted,
    guard_anthropic,
    guard_openai,
    no_sdk_retries,
    resume_safe,
    sdk_max_retries,
    seed_limiter,
    stream_transient,
)
from python_wrap.chaos import chaos
from python_wrap.concurrency import timeout
from python_wrap.doctor import doctor
from python_wrap.errors import RateLimitExceeded
from python_wrap.events import Event, listening
from python_wrap.llm import (
    Budget,
    BudgetExceeded,
    StreamTimeout,
    llm_retry,
    llm_transient,
    stream_timeout,
    token_rate_limit,
)
from python_wrap.llm_testing import RateLimitError, fake_llm
from python_wrap.policy import policy
from python_wrap.testing import FakeClock, disable_rate_limits

# ============================================================================ fakes


@contextlib.contextmanager
def recorded() -> Iterator[list[Event]]:
    """Collect published python-wrap events."""
    events: list[Event] = []
    with listening(events.append):
        yield events


class Headers(dict[str, str]):
    """Case-insensitive enough for the tests (the SDKs use httpx.Headers)."""


class FakeHTTPResponse:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.headers = Headers(headers or {})


class APIStatusError(Exception):
    """Shaped like ``openai.APIStatusError`` / ``anthropic.APIStatusError``."""

    def __init__(
        self, status_code: int, *, headers: dict[str, str] | None = None, body: Any = None
    ) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.response = FakeHTTPResponse(status_code, headers)
        self.body = body


class InternalServerError(APIStatusError):
    pass


class BadRequestError(APIStatusError):
    pass


def openai_usage(prompt: int, completion: int, cached: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached, audio_tokens=0),
    )


def anthropic_usage(inp: int, out: int, read: int = 0, write: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=write,
    )


def completion(text: str = "hi", prompt: int = 10, out: int = 2, cached: int = 0) -> Any:
    message = SimpleNamespace(role="assistant", content=text, tool_calls=None)
    return SimpleNamespace(
        id="chatcmpl-1",
        object="chat.completion",
        model="m-1",
        choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")],
        usage=openai_usage(prompt, out, cached),
    )


def message(text: str = "hi", inp: int = 10, out: int = 2, read: int = 0, write: int = 0) -> Any:
    return SimpleNamespace(
        id="msg_1",
        type="message",
        model="m-1",
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=anthropic_usage(inp, out, read, write),
    )


class RawResponse:
    """What ``resource.with_raw_response.create(...)`` returns."""

    def __init__(self, parsed: Any, headers: dict[str, str]) -> None:
        self._parsed = parsed
        self.headers = Headers(headers)

    def parse(self) -> Any:
        return self._parsed


class Resource:
    """``client.chat.completions`` / ``client.messages``: replies served in order.

    A reply is a value, an exception (raised) or ``(value, headers)``. Like the SDK, the
    resource retries 429/5xx itself ``owner.max_retries`` times. Copies made by
    ``with_options`` share the script and the counters.
    """

    def __init__(self, owner: FakeClient, replies: list[Any]) -> None:
        self.owner = owner
        self.replies = replies
        self.calls: list[dict[str, Any]] = []
        self.counter = [0]
        self.lock = threading.Lock()
        self.bind_raw()

    def bind_raw(self) -> None:
        self.with_raw_response = SimpleNamespace(create=self._raw_create)

    def bound(self, owner: FakeClient) -> Resource:
        clone = copy.copy(self)
        clone.owner = owner
        clone.bind_raw()
        return clone

    @property
    def http_requests(self) -> int:
        return self.counter[0]

    def _next(self, kwargs: dict[str, Any]) -> tuple[Any, dict[str, str]]:
        attempts = 1 + self.owner.max_retries
        for attempt in range(attempts):
            with self.lock:
                self.counter[0] += 1
                self.calls.append(kwargs)
                reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            if callable(reply) and not isinstance(reply, type):
                reply = reply(kwargs)
            headers: dict[str, str] = {}
            if isinstance(reply, tuple):
                reply, headers = reply
            if isinstance(reply, BaseException):
                if llm_transient(reply) and attempt < attempts - 1:
                    continue  # the SDK's own retry
                raise reply
            return reply, headers
        raise AssertionError("unreachable")

    def create(self, **kwargs: Any) -> Any:
        return self._next(kwargs)[0]

    def _raw_create(self, **kwargs: Any) -> RawResponse:
        value, headers = self._next(kwargs)
        return RawResponse(value, headers)

    def stream(self, **kwargs: Any) -> Any:
        stream, _ = self._next({**kwargs, "stream": True})
        return Manager(stream)

    def count_tokens(self, **kwargs: Any) -> Any:
        return SimpleNamespace(input_tokens=7)


class AsyncResource(Resource):
    def bind_raw(self) -> None:
        self.with_raw_response = SimpleNamespace(create=self._araw_create)

    async def create(self, **kwargs: Any) -> Any:  # type: ignore[override]
        await asyncio.sleep(0)
        return self._next(kwargs)[0]

    async def _araw_create(self, **kwargs: Any) -> RawResponse:
        await asyncio.sleep(0)
        value, headers = self._next(kwargs)
        return RawResponse(value, headers)

    def stream(self, **kwargs: Any) -> Any:
        stream, _ = self._next({**kwargs, "stream": True})
        return AsyncManager(stream)


class Manager:
    """``messages.stream(...)``: a context manager whose ``__enter__`` returns the stream."""

    def __init__(self, stream: Any) -> None:
        self.stream = stream
        self.exited = False

    def __enter__(self) -> Any:
        if isinstance(self.stream, BaseException):
            raise self.stream
        return self.stream

    def __exit__(self, *exc: object) -> None:
        self.exited = True


class AsyncManager(Manager):
    async def __aenter__(self) -> Any:
        return self.__enter__()

    async def __aexit__(self, *exc: object) -> None:
        self.__exit__()


class FakeClient:
    """Shaped like ``openai.OpenAI`` and ``anthropic.Anthropic`` at once."""

    resource_class: type[Resource] = Resource

    def __init__(self, replies: list[Any] | None = None, *, max_retries: int = 2) -> None:
        self.max_retries = max_retries
        self.attach(self.resource_class(self, list(replies or [completion()])))
        self.models = SimpleNamespace(list=lambda: ["m-1"])
        self.runner_messages: list[Any] = []
        self.beta = SimpleNamespace(
            messages=SimpleNamespace(tool_runner=self._tool_runner, batches="batches")
        )

    def attach(self, resource: Resource) -> None:
        self.resource = resource
        self.chat = SimpleNamespace(completions=resource)
        self.responses = resource
        self.messages = resource

    def with_options(self, *, max_retries: int) -> FakeClient:
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone.max_retries = max_retries
        clone.attach(self.resource.bound(clone))
        return clone

    def _tool_runner(self, **kwargs: Any) -> Any:
        return iter(self.runner_messages)


class AsyncFakeClient(FakeClient):
    """The class name starts with ``Async``, like ``AsyncOpenAI``."""

    resource_class = AsyncResource

    def _tool_runner(self, **kwargs: Any) -> Any:
        messages = self.runner_messages

        class Runner:
            def __aiter__(self) -> AsyncIterator[Any]:
                return self._gen()

            async def _gen(self) -> AsyncIterator[Any]:
                for item in messages:
                    yield item

        return Runner()


def ev(kind: str, **fields: Any) -> SimpleNamespace:
    """A typed stream event (Anthropic and OpenAI Responses shapes)."""
    return SimpleNamespace(type=kind, **fields)


def anthropic_events(text: str = "On it.", *, tool: bool = True) -> list[Any]:
    events = [
        ev("message_start", message=SimpleNamespace(model="m-1", usage=anthropic_usage(25, 1))),
        ev("content_block_start", index=0, content_block=SimpleNamespace(type="text", text="")),
        ev("content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text=text)),
        ev("content_block_stop", index=0),
    ]
    if tool:
        block = SimpleNamespace(type="tool_use", id="toolu_1", name="lookup", input={})
        events += [
            ev("content_block_start", index=1, content_block=block),
            ev(
                "content_block_delta",
                index=1,
                delta=SimpleNamespace(type="input_json_delta", partial_json='{"id": '),
            ),
            ev(
                "content_block_delta",
                index=1,
                delta=SimpleNamespace(type="input_json_delta", partial_json='"A-1"}'),
            ),
            ev("content_block_stop", index=1),
        ]
    events += [
        ev("message_delta", usage=SimpleNamespace(output_tokens=14)),
        ev("message_stop"),
    ]
    return events


def chat_chunk(
    content: str | None = None, tools: Any = None, finish: Any = None, usage: Any = None
) -> Any:
    delta = SimpleNamespace(role=None, content=content, tool_calls=tools)
    choices = [SimpleNamespace(index=0, delta=delta, finish_reason=finish)]
    return SimpleNamespace(
        object="chat.completion.chunk", model="m-1", choices=choices, usage=usage
    )


def tool_delta(
    index: int, *, id: str | None = None, name: str | None = None, args: str = ""
) -> Any:
    return SimpleNamespace(index=index, id=id, function=SimpleNamespace(name=name, arguments=args))


def chat_events() -> list[Any]:
    return [
        chat_chunk(content=""),  # role-only
        chat_chunk(content="Checking "),
        chat_chunk(tools=[tool_delta(0, id="call_1", name="lookup", args='{"id"')]),
        chat_chunk(tools=[tool_delta(0, args=': "A-1"}')]),
        chat_chunk(tools=[tool_delta(1, id="call_2", name="refund", args='{"id": "A-1"}')]),
        chat_chunk(finish="tool_calls"),
        SimpleNamespace(choices=[], model="m-1", usage=openai_usage(30, 9)),
    ]


def responses_events() -> list[Any]:
    item = SimpleNamespace(type="function_call", call_id="call_9", name="lookup", arguments="")
    done = SimpleNamespace(
        type="function_call", call_id="call_9", name="lookup", arguments='{"id": 1}'
    )
    return [
        ev("response.created", response=SimpleNamespace(model="m-1")),
        ev("response.in_progress"),
        ev("response.output_text.delta", delta="Let me check. "),
        ev("response.output_item.added", output_index=1, item=item),
        ev("response.function_call_arguments.delta", output_index=1, delta='{"id"'),
        ev("response.function_call_arguments.delta", output_index=1, delta=": 1}"),
        ev("response.function_call_arguments.done", output_index=1, arguments='{"id": 1}'),
        ev("response.output_item.done", output_index=1, item=done),
        ev(
            "response.completed",
            response=SimpleNamespace(
                model="m-1",
                usage=SimpleNamespace(
                    input_tokens=40,
                    output_tokens=6,
                    input_tokens_details=SimpleNamespace(cached_tokens=0),
                ),
            ),
        ),
    ]


class Script:
    """A sync/async iterable of events that can fail after ``fail_after`` events."""

    def __init__(
        self, events: list[Any], *, fail_after: int | None = None, error: Any = None
    ) -> None:
        self.events = events
        self.fail_after = fail_after
        self.error = error if error is not None else ConnectionResetError("reset")
        self.closed = False

    def __iter__(self) -> Iterator[Any]:
        for index, event in enumerate(self.events):
            if index == self.fail_after:
                raise self.error
            yield event
        if self.fail_after is not None and self.fail_after >= len(self.events):
            raise self.error

    async def __aiter__(self) -> AsyncIterator[Any]:
        for event in self.__iter__():
            await asyncio.sleep(0)
            yield event

    def close(self) -> None:
        self.closed = True


def opener(*streams: Any) -> Any:
    """A factory returning the given streams (or raising given errors) in order."""
    remaining = list(streams)
    opened: list[Any] = []

    def factory() -> Any:
        item = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        opened.append(item)
        if isinstance(item, BaseException):
            raise item
        return item

    factory.opened = opened  # type: ignore[attr-defined]
    return factory


# ===================================================================== SDK retries


def test_no_sdk_retries_uses_with_options_and_keeps_the_original() -> None:
    client = FakeClient()
    quiet = no_sdk_retries(client)
    assert (client.max_retries, quiet.max_retries) == (2, 0)
    assert quiet.resource.replies is client.resource.replies  # shares the "connection"


def test_no_sdk_retries_falls_back_to_copy_and_refuses_unknown_clients() -> None:
    class Copyable:
        max_retries = 3

        def copy(self, *, max_retries: int) -> SimpleNamespace:
            return SimpleNamespace(max_retries=max_retries)

    assert no_sdk_retries(Copyable()).max_retries == 0
    with pytest.raises(TypeError, match="max_retries=0"):
        no_sdk_retries(object())


def test_sdk_max_retries_reads_clients_guards_and_unknowns() -> None:
    client = FakeClient(max_retries=5)
    assert sdk_max_retries(client) == 5
    assert sdk_max_retries(guard_openai(client).client) == 0
    assert sdk_max_retries(object()) is None
    assert sdk_max_retries(SimpleNamespace(max_retries=True)) is None


# ============================================================ llm_retry ownership


class Overloaded(Exception):
    status_code = 529


def test_owner_makes_nested_retries_single_attempt(fake_clock: FakeClock) -> None:
    calls = []

    @llm_retry(max_attempts=3, delay=1, jitter=0)
    def inner() -> str:
        calls.append(1)
        raise Overloaded

    @llm_retry(max_attempts=4, delay=1, jitter=0, owns_retries=True)
    def outer() -> str:
        return inner()

    with pytest.raises(Overloaded):
        outer()
    assert len(calls) == 4  # not 4 x 3 = 12

    calls.clear()
    with pytest.raises(Overloaded):
        inner()  # outside any owner it retries normally again
    assert len(calls) == 3


def test_owner_inside_a_plain_loop_makes_a_single_attempt(fake_clock: FakeClock) -> None:
    calls = []

    @llm_retry(max_attempts=3, delay=1, jitter=0, owns_retries=True)
    def inner() -> None:
        calls.append(1)
        raise Overloaded

    @llm_retry(max_attempts=2, delay=1, jitter=0)
    def outer() -> None:
        inner()

    with pytest.raises(Overloaded):
        outer()
    assert len(calls) == 2


def test_plain_retries_still_multiply_without_an_owner(fake_clock: FakeClock) -> None:
    calls = []

    @llm_retry(max_attempts=2, delay=1, jitter=0)
    def inner() -> None:
        calls.append(1)
        raise Overloaded

    @llm_retry(max_attempts=2, delay=1, jitter=0)
    def outer() -> None:
        inner()

    with pytest.raises(Overloaded):
        outer()
    assert len(calls) == 4  # unchanged default behaviour


async def test_owner_works_for_async_functions(fake_clock: FakeClock) -> None:
    calls = []

    @llm_retry(max_attempts=3, delay=1, jitter=0)
    async def inner() -> None:
        calls.append(1)
        raise Overloaded

    @llm_retry(max_attempts=2, delay=1, jitter=0, owns_retries=True)
    async def outer() -> None:
        await inner()

    with pytest.raises(Overloaded):
        await outer()
    assert len(calls) == 2


def test_owner_scope_is_per_task_and_thread(fake_clock: FakeClock) -> None:
    seen: list[int] = []

    @llm_retry(max_attempts=3, delay=1, jitter=0)
    def other() -> None:
        seen.append(1)
        raise Overloaded

    def in_thread() -> None:
        with contextlib.suppress(Overloaded):
            other()

    @llm_retry(max_attempts=2, delay=1, jitter=0, owns_retries=True)
    def owner() -> None:
        thread = threading.Thread(target=in_thread)
        thread.start()
        thread.join()
        raise Overloaded

    with pytest.raises(Overloaded):
        owner()
    assert len(seen) == 2 * 3  # a new thread does not inherit the owner scope


def test_llm_retry_settings_and_stats_survive_the_owner_wrapper() -> None:
    @llm_retry(max_attempts=2, owns_retries=True, client=FakeClient(max_retries=3))
    def call() -> str:
        return "ok"

    assert call() == "ok"
    kind, settings = call.__python_wrap_layer__  # type: ignore[attr-defined]
    assert kind == "retry"
    assert settings["owns_retries"] is True and settings["sdk_retries"] == 3
    assert hasattr(call, "__retry_stats__")
    assert call.__wrapped__.__name__ == "call"  # type: ignore[attr-defined]


# ========================================================================== doctor


def _codes(target: Any) -> list[str]:
    return [finding.code for finding in doctor(target).findings]


def test_pw018_sdk_retries_under_python_wrap_retry() -> None:
    client = FakeClient(max_retries=2)

    @Budget(tokens=100)
    @llm_retry(max_attempts=4, client=client)
    @stream_timeout(first_token=5, chunk_gap=5)
    def ask() -> Any:
        yield 1

    report = doctor(ask)
    pw018 = [f for f in report.findings if f.code == "PW018"]
    assert pw018 and "4 x 3 = 12" in pw018[0].message

    @Budget(tokens=100)
    @llm_retry(max_attempts=4, client=no_sdk_retries(client))
    def quiet() -> None: ...

    assert "PW018" not in _codes(quiet)


def test_pw019_retry_around_a_stream_without_resume_safe() -> None:
    @Budget(tokens=100)
    @llm_retry(max_attempts=3)
    @stream_timeout(first_token=5, chunk_gap=5)
    def risky() -> Any:
        yield 1

    assert "PW019" in _codes(risky)

    @Budget(tokens=100)
    @llm_retry(max_attempts=3)
    @resume_safe(first_token=5, chunk_gap=5)
    @stream_timeout(first_token=5, chunk_gap=5)
    def safe() -> Any:
        yield 1

    assert "PW019" not in _codes(safe)


def test_guard_doctor_report_has_budget_and_retry_but_no_pw018() -> None:
    guarded = guard_openai(
        FakeClient(), budget=Budget(tokens=10), policy=policy(timeout=timeout(30))
    )
    report = guarded.doctor()
    roles = [layer.role for layer in report.layers]
    assert "budget" in roles and "retry" in roles
    assert "PW018" not in [f.code for f in report.findings]
    assert "PW013" not in [f.code for f in report.findings]


# ====================================================================== the guard


def test_guard_retries_transient_errors_once_per_layer(fake_clock: FakeClock) -> None:
    client = FakeClient([InternalServerError(500), InternalServerError(503), completion()])
    guarded = guard_openai(client, attempts=4)
    result = guarded.chat.completions.create(model="m-1", messages=[])
    assert result.choices[0].message.content == "hi"
    assert client.resource.http_requests == 3  # the SDK copy has max_retries=0
    assert isinstance(guarded, GuardedOpenAI)


def test_guard_never_retries_bad_requests(fake_clock: FakeClock) -> None:
    client = FakeClient([BadRequestError(400)])
    with pytest.raises(BadRequestError):
        guard_openai(client).chat.completions.create(model="m-1", messages=[])
    assert client.resource.http_requests == 1


def test_guard_attempts_are_bounded_even_when_everything_fails(fake_clock: FakeClock) -> None:
    client = FakeClient([InternalServerError(500)])
    with pytest.raises(InternalServerError):
        guard_anthropic(client, attempts=3).messages.create(model="m-1", messages=[])
    assert client.resource.http_requests == 3


def test_owns_retries_false_leaves_retries_to_the_sdk(fake_clock: FakeClock) -> None:
    client = FakeClient([InternalServerError(500), InternalServerError(500), completion()])
    guarded = guard_openai(client, owns_retries=False)
    assert guarded.client is client and guarded.attempts == 1
    guarded.chat.completions.create(model="m-1", messages=[])
    assert client.resource.http_requests == 3  # all of them the SDK's own retries
    assert "retry" not in [layer.role for layer in guarded.doctor().layers]


def test_guard_charges_the_budget_and_refuses_when_spent() -> None:
    client = FakeClient([completion(prompt=60, out=40)])
    spend = Budget(tokens=150)
    guarded = guard_openai(client, budget=spend)
    guarded.chat.completions.create(model="m-1", messages=[])
    guarded.chat.completions.create(model="m-1", messages=[])  # crosses the limit
    with pytest.raises(BudgetExceeded):
        guarded.chat.completions.create(model="m-1", messages=[])
    assert client.resource.http_requests == 2 and spend.spent().tokens == 200


def test_guard_budget_per_key_receives_the_sdk_kwargs() -> None:
    spend = Budget(tokens=15, per="key", key=lambda **kw: kw["user"])
    guarded = guard_anthropic(FakeClient([message(inp=10, out=6)]), budget=spend)
    guarded.messages.create(model="m-1", messages=[], user="ann")
    with pytest.raises(BudgetExceeded):
        guarded.messages.create(model="m-1", messages=[], user="ann")
    guarded.messages.create(model="m-1", messages=[], user="bob")
    assert spend.spent(key="bob").tokens == 16


def test_guard_reads_anthropic_usage_including_cache_tokens() -> None:
    spend = Budget(tokens=10_000)
    guarded = guard_anthropic(
        FakeClient([message(inp=10, out=5, read=100, write=20)]), budget=spend
    )
    guarded.messages.create(model="m-1", messages=[])
    assert spend.spent().tokens == 135
    assert isinstance(guarded, GuardedAnthropic)


def test_guard_limiter_settles_with_real_usage_and_follows_headers(fake_clock: FakeClock) -> None:
    headers = {"x-ratelimit-remaining-tokens": "300"}
    client = FakeClient([(completion(prompt=50, out=10), headers)])
    limiter = token_rate_limit(10_000, estimate=1_000)
    guarded = guard_openai(client, limiter=limiter)
    guarded.chat.completions.create(model="m-1", messages=[])
    assert limiter.available == pytest.approx(300)


def test_guard_limiter_drains_on_429_retry_after(fake_clock: FakeClock) -> None:
    limited = RateLimitError(retry_after=30)  # look-alike with Retry-After header
    client = FakeClient([limited, completion()])
    limiter = token_rate_limit(6_000, estimate=10, block=False)
    guarded = guard_openai(client, limiter=limiter, retry={"max_delay": 1})
    with pytest.raises(RateLimitExceeded) as caught:
        guarded.chat.completions.create(model="m-1", messages=[])
    assert caught.value.retry_after >= 29  # the next reservation must wait out the 429


def test_guard_without_raw_responses_or_headers_still_works() -> None:
    client = FakeClient([completion()])
    del client.resource.with_raw_response
    limiter = token_rate_limit(1_000, estimate=100)
    guard_openai(client, limiter=limiter).chat.completions.create(model="m-1", messages=[])
    assert limiter.available == pytest.approx(1_000 - 12)
    other = FakeClient([(completion(), {"x-ratelimit-remaining-tokens": "1"})])
    limiter2 = token_rate_limit(1_000, estimate=100)
    guard_openai(other, limiter=limiter2, read_headers=False).chat.completions.create(model="m")
    assert limiter2.available > 900  # headers were not read


def test_guard_policy_runs_per_attempt(fake_clock: FakeClock) -> None:
    attempts = []

    def count(func: Any) -> Any:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempts.append(1)
            return func(*args, **kwargs)

        return wrapper

    client = FakeClient([InternalServerError(500), completion()])
    guard_openai(client, policy=count).chat.completions.create(model="m-1", messages=[])
    assert len(attempts) == 2


def test_guard_trace_publishes_genai_events() -> None:
    from python_wrap.genai import instrument

    with instrument(otel=False), recorded() as events:
        guard_openai(FakeClient([completion()]), trace=True).chat.completions.create(model="m-1")
    calls = [e for e in events if e.name == "genai.llm_call"]
    assert calls and calls[0].fields["system"] == "openai"
    assert calls[0].fields["input_tokens"] == 10


def test_guard_passes_unguarded_attributes_to_the_original_client() -> None:
    client = FakeClient()
    guarded = guard_anthropic(client)
    assert guarded.models.list() == ["m-1"]
    assert guarded.messages.count_tokens(messages=[]).input_tokens == 7
    assert guarded.beta.messages.batches == "batches"
    assert guarded.chat.completions is client.resource  # anthropic guard: not wrapped
    with pytest.raises(AttributeError):
        guarded._private  # noqa: B018
    assert "GuardedAnthropic" in repr(guarded)


def test_guard_validates_options() -> None:
    with pytest.raises(ValueError):
        guard_openai(FakeClient(), attempts=0)
    with pytest.raises(ValueError):
        guard_openai(FakeClient(), retry={"max_attempts": 3})
    with pytest.raises(ValueError):
        guard_openai(FakeClient(), chunk_gap=-1)


def test_guard_refuses_a_retrying_client_it_cannot_quiet() -> None:
    class Stubborn:
        max_retries = 2

    with pytest.raises(TypeError):
        guard_openai(Stubborn())
    assert guard_openai(SimpleNamespace(chat=None)).attempts == 4  # no SDK retries: fine


async def test_async_guard_create_retry_budget_and_limiter(fake_clock: FakeClock) -> None:
    client = AsyncFakeClient(
        [InternalServerError(500), (completion(), {"x-ratelimit-remaining-tokens": "50"})]
    )
    spend = Budget(tokens=100)
    limiter = token_rate_limit(1_000, estimate=100)
    guarded = guard_openai(client, budget=spend, limiter=limiter)
    assert guarded.is_async
    result = await guarded.chat.completions.create(model="m-1", messages=[])
    assert result.choices[0].message.content == "hi"
    assert client.resource.http_requests == 2
    assert spend.spent().tokens == 12
    assert limiter.available == pytest.approx(50)


async def test_async_guard_with_fake_provider_and_chaos(fake_clock: FakeClock) -> None:
    llm = fake_llm(["answer"], usage=(7, 3), style="openai")
    guarded = guard_openai(llm.aio, is_async=True, budget=Budget(tokens=100))
    with chaos.llm.rate_limit("llm.*", times=2, retry_after=1) as rule:
        result = await guarded.chat.completions.create(model="m-1", messages=[])
    assert result.text == "answer" and rule.injected == 2
    assert llm.call_count == 3 and llm.served_count == 1
    assert fake_clock.sleeps == [1.0, 1.0]


def test_fake_provider_with_chaos_overloaded_and_server_errors(fake_clock: FakeClock) -> None:
    llm = fake_llm(["done"], usage=(5, 5))
    spend = Budget(tokens=1_000)
    guarded = guard_anthropic(llm, budget=spend, attempts=4)
    with chaos.llm.overloaded("llm.*", times=1), chaos.llm.server_error("llm.*", times=1):
        assert guarded.messages.create(model="m-1", messages=[]).text == "done"
    assert llm.call_count == 3 and spend.spent().tokens == 10
    with chaos.llm.overloaded("llm.*"), pytest.raises(Exception, match="Overloaded"):
        guarded.messages.create(model="m-1", messages=[])
    assert llm.call_count == 3 + 4  # bounded by attempts


def test_fake_provider_stream_through_the_guard_with_chaos(fake_clock: FakeClock) -> None:
    llm = fake_llm(["the answer is forty-two"], usage=(4, 6), style="openai")
    spend = Budget(tokens=1_000)
    guarded = guard_openai(llm, budget=spend)
    with chaos.llm.rate_limit("llm.*", times=1, retry_after=2):
        stream = guarded.chat.completions.create(model="m-1", messages=[], stream=True)
        text = "".join(chunk.text for chunk in stream)
    assert text == "the answer is forty-two"
    assert stream.attempts == 2 and spend.spent().tokens == 10
    assert llm.calls[-1].kwargs["stream_options"] == {"include_usage": True}


# ======================================================================= streams


def test_stream_failing_before_output_is_reopened_without_duplicates(fake_clock: FakeClock) -> None:
    events = anthropic_events()
    factory = opener(Script(events, fail_after=1), Script(events))
    stream = resume_safe(factory)
    received = list(stream)
    assert received == events  # the first attempt's message_start was held back
    assert stream.attempts == 2 and len(factory.opened) == 2
    assert factory.opened[0].closed
    assert stream.text == "On it."
    [call] = stream.tool_calls
    assert (call.id, call.name, call.input) == ("toolu_1", "lookup", {"id": "A-1"})
    assert stream.usage is not None and stream.usage.total_tokens == 25 + 14
    assert stream.model == "m-1"


def test_stream_open_errors_are_retried_with_backoff(fake_clock: FakeClock) -> None:
    factory = opener(InternalServerError(503), InternalServerError(502), Script(["a", "b"]))
    with recorded() as events:
        stream = resume_safe(factory, delay=1.0)
        assert list(stream) == ["a", "b"]
    assert fake_clock.sleeps == [1.0, 2.0]
    resumed = [e for e in events if e.name == "llm.stream_resumed"]
    assert [e.fields["attempt"] for e in resumed] == [1, 2]


def test_stream_retry_after_hint_is_honoured_and_capped(fake_clock: FakeClock) -> None:
    hinted = APIStatusError(429, headers={"retry-after": "3"})
    capped = APIStatusError(429, headers={"retry-after": "120"})
    stream = resume_safe(opener(hinted, capped, Script(["x"])), max_delay=10, max_attempts=3)
    assert list(stream) == ["x"]
    assert fake_clock.sleeps == [3.0, 10.0]


def test_stream_failing_after_text_raises_interrupted(fake_clock: FakeClock) -> None:
    events = anthropic_events()
    factory = opener(Script(events, fail_after=6), Script(events))
    stream = resume_safe(factory)
    received = []
    with recorded() as published, pytest.raises(StreamInterrupted) as caught:
        for event in stream:
            received.append(event)
    error = caught.value
    assert received == events[:6]
    assert error.text == "On it." and error.tool_calls == ()
    assert [(p.id, p.arguments) for p in error.pending] == [("toolu_1", '{"id": ')]
    assert error.attempts == 1 and len(factory.opened) == 1  # never replayed
    assert isinstance(error.__cause__, ConnectionResetError)
    assert error.usage is not None and error.usage.input_tokens == 25
    assert not llm_transient(error) and not stream_transient(error)
    assert [e.name for e in published] == ["llm.stream_interrupted"]


def test_completed_tool_calls_are_reported_when_the_stream_dies_after_them() -> None:
    events = chat_events()
    stream = resume_safe(opener(Script(events, fail_after=6)))
    with pytest.raises(StreamInterrupted) as caught:
        list(stream)
    calls = caught.value.tool_calls
    assert [(c.id, c.name, c.input) for c in calls] == [
        ("call_1", "lookup", {"id": "A-1"}),
        ("call_2", "refund", {"id": "A-1"}),
    ]
    assert caught.value.text == "Checking " and caught.value.pending == ()


def test_openai_chat_stream_tracking_and_role_only_chunk_is_not_output() -> None:
    events = chat_events()
    stream = resume_safe(opener(Script(events, fail_after=1), Script(events)))
    assert list(stream) == events
    assert stream.attempts == 2
    assert [c.name for c in stream.tool_calls] == ["lookup", "refund"]
    assert stream.usage is not None and stream.usage.total_tokens == 39


def test_openai_responses_stream_tracking() -> None:
    events = responses_events()
    stream = resume_safe(opener(Script(events, fail_after=2), Script(events)))
    assert list(stream) == events and stream.attempts == 2
    assert stream.text == "Let me check. "
    [call] = stream.tool_calls
    assert (call.id, call.name, call.input) == ("call_9", "lookup", {"id": 1})
    assert stream.usage is not None and stream.usage.total_tokens == 46
    assert stream.usage_fields["cached_tokens"] == 0


def test_responses_stream_dying_mid_arguments_reports_pending() -> None:
    stream = resume_safe(opener(Script(responses_events(), fail_after=5)))
    with pytest.raises(StreamInterrupted) as caught:
        list(stream)
    assert [p.arguments for p in caught.value.pending] == ['{"id"']


def test_non_transient_errors_before_output_are_raised_as_is() -> None:
    factory = opener(Script(["a"], fail_after=0, error=ValueError("bug")), Script(["a"]))
    stream = resume_safe(factory)
    with pytest.raises(ValueError, match="bug"):
        list(stream)
    assert stream.attempts == 1


def test_attempts_are_bounded_when_every_open_fails(fake_clock: FakeClock) -> None:
    factory = opener(InternalServerError(500))
    stream = resume_safe(factory, max_attempts=3)
    with pytest.raises(InternalServerError):
        list(stream)
    assert stream.attempts == 3 and len(factory.opened) == 3


def test_stall_before_first_event_is_reopened_with_stream_timeout() -> None:
    gate = threading.Event()

    class Stalls:
        def __iter__(self) -> Iterator[str]:
            gate.wait(2)
            yield "late"

    stream = resume_safe(opener(Stalls(), Script(["fast"])), first_token=0.05, delay=0)
    try:
        assert list(stream) == ["fast"]
    finally:
        gate.set()
    assert stream.attempts == 2


def test_stall_after_output_raises_interrupted_with_stream_timeout() -> None:
    gate = threading.Event()

    class StallsLater:
        def __iter__(self) -> Iterator[str]:
            yield "Hel"
            gate.wait(2)
            yield "lo"

    stream = resume_safe(opener(StallsLater()), chunk_gap=0.05)
    try:
        with pytest.raises(StreamInterrupted) as caught:
            list(stream)
    finally:
        gate.set()
    assert caught.value.text == "Hel" and isinstance(caught.value.__cause__, StreamTimeout)


def test_empty_stream_and_prelude_only_stream_are_handed_out_at_the_end() -> None:
    prelude = [ev("message_start", message=SimpleNamespace(usage=anthropic_usage(3, 0)))]
    stream = resume_safe(opener(Script(prelude)))
    assert list(stream) == prelude and not stream.committed
    assert list(resume_safe(opener(Script([])))) == []


def test_stream_can_be_iterated_once_and_proxies_attributes() -> None:
    script = Script(["a"])
    script.response = "raw"  # type: ignore[attr-defined]
    stream: ResumableStream[str] = resume_safe(opener(script), name="answers")
    with pytest.raises(AttributeError):
        stream.response  # noqa: B018 - nothing opened yet
    iterator = iter(stream)
    assert next(iterator) == "a"
    assert stream.response == "raw"  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError):
        iter(stream)
    assert "answers" in repr(stream)


def test_breaking_out_closes_the_underlying_stream_and_finishes_once() -> None:
    finished: list[Any] = []
    script = Script(["a", "b", "c"])
    stream = ResumableStream(opener(script), on_finish=lambda s, e: finished.append(e))
    for item in stream:
        if item == "a":
            break
    stream.close()
    assert script.closed and len(finished) == 1


def test_close_before_iterating_runs_the_finish_hook() -> None:
    finished: list[Any] = []
    with ResumableStream(opener(Script(["a"])), on_finish=lambda s, e: finished.append(e)):
        pass
    assert finished == [None]


def test_finish_hook_errors_are_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    def broken(stream: Any, error: Any) -> None:
        raise RuntimeError("hook")

    assert list(ResumableStream(opener(Script(["a"])), on_finish=broken)) == ["a"]
    assert "finish hook failed" in caplog.text


def test_context_manager_factories_are_entered_and_exited() -> None:
    manager = Manager(Script(anthropic_events(tool=False)))
    stream = resume_safe(lambda: manager)
    assert [e.type for e in stream][-1] == "message_stop"
    assert manager.exited


def test_text_stream_yields_only_text_pieces() -> None:
    stream = resume_safe(opener(Script(anthropic_events())))
    assert list(stream.text_stream) == ["On it."]
    derived = [*anthropic_events(tool=False)]
    derived.insert(3, ev("text", text="On it.", snapshot="On it."))  # SDK helper event
    stream2 = resume_safe(opener(Script(derived)))
    assert list(stream2.text_stream) == ["On it."]


def test_async_factory_on_a_sync_iteration_is_refused() -> None:
    async def opens() -> Any:
        return Script(["a"])

    with pytest.raises(TypeError, match="async for"):
        list(resume_safe(opens))


def test_resume_safe_validation() -> None:
    for bad in ({"max_attempts": 0}, {"max_attempts": True}, {"delay": -1}, {"chunk_gap": 0}):
        with pytest.raises(ValueError):
            resume_safe(opener(Script([])), **bad)
        with pytest.raises(ValueError):
            resume_safe(**bad)


def test_resume_safe_decorator_reopens_by_calling_again(fake_clock: FakeClock) -> None:
    calls: list[str] = []

    @resume_safe(max_attempts=2)
    def ask(prompt: str) -> Any:
        calls.append(prompt)
        if len(calls) == 1:
            raise InternalServerError(503)
        return Script([prompt.upper()])

    assert list(ask("hi")) == ["HI"]
    assert calls == ["hi", "hi"]
    kind, settings = ask.__python_wrap_layer__  # type: ignore[attr-defined]
    assert kind == "guard" and settings["llm"] == "resume_safe"


async def test_async_stream_reopen_and_interrupt(fake_clock: FakeClock) -> None:
    events = anthropic_events()

    async def opens_async() -> Any:
        return next(scripts)

    scripts = iter([Script(events, fail_after=1), Script(events)])
    stream = resume_safe(opens_async)
    assert [e async for e in stream] == events and stream.attempts == 2

    broken = resume_safe(lambda: Script(events, fail_after=4))
    with pytest.raises(StreamInterrupted) as caught:
        async for _ in broken:
            pass
    assert caught.value.text == "On it."


async def test_async_text_stream_and_context_managers() -> None:
    manager = AsyncManager(Script(anthropic_events()))
    stream = resume_safe(lambda: manager)
    assert [text async for text in stream.text_stream] == ["On it."]
    assert manager.exited
    async with resume_safe(lambda: Script(["a"])) as unused:
        assert isinstance(unused, ResumableStream)


async def test_async_stall_is_reopened() -> None:
    class Stalls:
        async def __aiter__(self) -> AsyncIterator[str]:
            await asyncio.sleep(5)
            yield "late"

    stream = resume_safe(opener(Stalls(), Script(["fast"])), first_token=0.05, delay=0)
    assert [x async for x in stream] == ["fast"]


async def test_async_iteration_of_a_sync_only_stream_is_refused() -> None:
    class SyncOnly:
        def __iter__(self) -> Iterator[str]:
            yield "a"

    with pytest.raises(TypeError, match="iterate it with `for`"):
        [x async for x in resume_safe(lambda: SyncOnly())]


def test_stream_transient_classification() -> None:
    class ReadError(Exception):
        pass

    in_stream = APIStatusError(200, body={"type": "error", "error": {"type": "overloaded_error"}})
    refusal = APIStatusError(200, body={"error": {"code": "content_filter"}})
    assert stream_transient(ReadError("reset"))
    assert stream_transient(in_stream) and not llm_transient(in_stream)
    assert stream_transient(StreamTimeout("slow", seconds=1, limit="first_token"))
    assert not stream_transient(refusal)
    assert not stream_transient(ValueError("bug"))


# ================================================================ guarded streams


def test_guarded_create_stream_charges_usage_at_the_end(fake_clock: FakeClock) -> None:
    client = FakeClient([InternalServerError(503), Script(chat_events())])
    spend = Budget(tokens=1_000)
    limiter = token_rate_limit(10_000, estimate=500)
    guarded = guard_openai(client, budget=spend, limiter=limiter, retry={"delay": 0})
    stream = guarded.chat.completions.create(model="m-1", messages=[], stream=True)
    assert limiter.available == pytest.approx(9_500)  # reserved once for the whole stream
    assert len(list(stream)) == len(chat_events())
    assert stream.attempts == 2
    assert spend.spent().tokens == 39
    assert limiter.available == pytest.approx(10_000 - 39)
    assert client.resource.calls[-1]["stream_options"] == {"include_usage": True}


def test_guarded_stream_keeps_explicit_stream_options() -> None:
    client = FakeClient([Script(["x"])])
    stream = guard_openai(client).chat.completions.create(
        model="m-1", messages=[], stream=True, stream_options={"include_usage": False}
    )
    assert list(stream) == ["x"]
    assert client.resource.calls[-1]["stream_options"] == {"include_usage": False}


def test_guarded_stream_refunds_a_stream_that_never_produced(fake_clock: FakeClock) -> None:
    client = FakeClient([InternalServerError(500)])
    limiter = token_rate_limit(1_000, estimate=400)
    stream = guard_openai(client, limiter=limiter, attempts=2).chat.completions.create(
        model="m-1", stream=True
    )
    with pytest.raises(InternalServerError):
        list(stream)
    assert limiter.available == pytest.approx(1_000)


def test_guarded_stream_budget_is_checked_before_opening() -> None:
    spend = Budget(tokens=10)
    spend.record({"usage": {"input_tokens": 10, "output_tokens": 0}})
    client = FakeClient([Script(["x"])])
    with pytest.raises(BudgetExceeded):
        guard_openai(client, budget=spend).chat.completions.create(model="m-1", stream=True)
    assert client.resource.http_requests == 0


def test_guarded_stream_interrupted_still_charges_reported_usage() -> None:
    events = anthropic_events()
    client = FakeClient([Script(events, fail_after=7)])
    spend = Budget(tokens=1_000)
    stream = guard_anthropic(client, budget=spend).messages.create(model="m-1", stream=True)
    with pytest.raises(StreamInterrupted):
        list(stream)
    assert spend.spent().tokens == 26  # message_start usage: input billed, output so far


def test_guarded_messages_stream_manager_sync(fake_clock: FakeClock) -> None:
    client = FakeClient([InternalServerError(529), Script(anthropic_events())])
    spend = Budget(tokens=1_000)
    guarded = guard_anthropic(client, budget=spend)
    with guarded.messages.stream(model="m-1", max_tokens=10, messages=[]) as stream:
        assert list(stream.text_stream) == ["On it."]
    assert stream.attempts == 2 and spend.spent().tokens == 39


async def test_guarded_async_create_stream_and_stream_manager(fake_clock: FakeClock) -> None:
    client = AsyncFakeClient([Script(anthropic_events())])
    spend = Budget(tokens=1_000)
    guarded = guard_anthropic(client, budget=spend)
    stream = await guarded.messages.create(model="m-1", stream=True)
    assert len([e async for e in stream]) == len(anthropic_events())
    async with guarded.messages.stream(model="m-1", messages=[]) as managed:
        texts = [t async for t in managed.text_stream]
    assert texts == ["On it."] and spend.spent().tokens == 78


def test_guarded_stream_reopen_respects_retry_if_option(fake_clock: FakeClock) -> None:
    client = FakeClient([InternalServerError(503), Script(["x"])])
    guarded = guard_openai(client, retry={"retry_if": lambda exc: False})
    stream = guarded.chat.completions.create(model="m-1", stream=True)
    with pytest.raises(InternalServerError):
        list(stream)
    assert stream.attempts == 1


# ======================================================================= runner


def test_tool_runner_steps_are_budgeted_and_rate_limited() -> None:
    client = FakeClient()
    client.runner_messages = [message("calling", inp=30, out=5), message("done", inp=40, out=6)]
    spend = Budget(tokens=1_000)
    limiter = token_rate_limit(10_000, estimate=100)
    runner = guard_anthropic(client, budget=spend, limiter=limiter).beta.messages.tool_runner(
        model="m-1", messages=[], tools=[]
    )
    assert runner.until_done().content[0].text == "done"
    assert runner.steps == 2 and spend.spent().tokens == 81
    assert limiter.available == pytest.approx(10_000 - 81)


def test_tool_runner_stops_at_the_budget_between_steps() -> None:
    client = FakeClient()
    client.runner_messages = [message(inp=60, out=0)] * 5
    spend = Budget(tokens=100)
    runner = guard_anthropic(client, budget=spend).beta.messages.tool_runner(model="m-1")
    with pytest.raises(BudgetExceeded):
        for _ in runner:
            pass
    assert runner.steps == 2


def test_tool_runner_uses_the_original_client_and_refunds_failures() -> None:
    class Failing:
        def __iter__(self) -> Iterator[Any]:
            raise InternalServerError(500)

    client = FakeClient()
    client.beta.messages.tool_runner = lambda **kw: Failing()
    limiter = token_rate_limit(1_000, estimate=200)
    runner = guard_anthropic(client, limiter=limiter).beta.messages.tool_runner(model="m-1")
    with pytest.raises(InternalServerError):
        list(runner)
    assert limiter.available == pytest.approx(1_000)


async def test_async_tool_runner() -> None:
    client = AsyncFakeClient()
    client.runner_messages = [message("a", inp=5, out=5), message("b", inp=5, out=5)]
    spend = Budget(tokens=100)
    runner = guard_anthropic(client, budget=spend).beta.messages.tool_runner(model="m-1")
    final = await runner.until_done()
    assert final.content[0].text == "b" and spend.spent().tokens == 20


# ===================================================================== limiter


def test_seed_limiter_only_lowers_the_bucket(fake_clock: FakeClock) -> None:
    limiter = token_rate_limit(1_000)
    seed_limiter(limiter, {"anthropic-ratelimit-tokens-remaining": "5000"})
    assert limiter.available == pytest.approx(1_000)
    seed_limiter(limiter, [("Anthropic-RateLimit-Input-Tokens-Remaining", "200")])
    assert limiter.available == pytest.approx(200)
    seed_limiter(limiter, {"x-ratelimit-remaining-tokens": "not a number"})
    seed_limiter(limiter, None)
    assert limiter.available == pytest.approx(200)


def test_seed_limiter_retry_after_makes_the_next_call_wait(fake_clock: FakeClock) -> None:
    limiter = token_rate_limit(600)  # 10 tokens / second
    seed_limiter(limiter, retry_after=5)
    assert limiter.try_reserve(1) >= 5
    seed_limiter(limiter, retry_after=float("nan"))  # ignored


# ================================================================== CacheGuard


def _anthropic_reply(read: int, write: int = 0, inp: int = 50) -> Any:
    return SimpleNamespace(usage=anthropic_usage(inp, 10, read, write))


def test_cache_guard_hit_rate_anthropic_and_openai() -> None:
    guard = CacheGuard(min_calls=1, min_prompt_tokens=100)
    guard.observe(_anthropic_reply(read=950, inp=50))
    stats = guard.stats()
    assert stats.prompt_tokens == 1000 and stats.hit_rate == pytest.approx(0.95)
    openai_guard = CacheGuard(min_calls=1, min_prompt_tokens=100)
    openai_guard.observe(completion(prompt=2000, cached=1024))
    assert openai_guard.stats().cache_read_tokens == 1024
    assert openai_guard.stats().hit_rate == pytest.approx(0.512)


def test_cache_guard_warns_once_with_reasons_and_event() -> None:
    guard = CacheGuard(min_calls=3, min_prompt_tokens=100, name="support-bot")
    request = {"system": [{"type": "text", "text": "Policy"}], "messages": []}
    with recorded() as events, warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(6):
            guard.observe(_anthropic_reply(read=0, write=0, inp=2000), request)
    assert len([w for w in caught if w.category is PromptCacheWarning]) == 1
    cold = [e for e in events if e.name == "llm.prompt_cache_cold"]
    assert len(cold) == 1 and cold[0].function == "support-bot"
    assert any("cache_control" in reason for reason in cold[0].fields["reasons"])


def test_cache_guard_detects_volatile_prefixes() -> None:
    guard = CacheGuard(min_calls=2, min_prompt_tokens=10)
    prefixes = [
        "Now: 2026-11-11T10:00:00Z. You are support.",
        "Now: 2026-11-11T10:00:05Z. You are support.",
    ]
    with recorded() as events, warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for prefix in prefixes:
            guard.observe(
                completion(prompt=2000), {"messages": [{"role": "system", "content": prefix}]}
            )
    assert "timestamp" in guard.volatile
    assert [e.name for e in events] == ["llm.prompt_cache_volatile", "llm.prompt_cache_cold"]
    messages = [str(w.message) for w in caught]
    assert "timestamp" in messages[0]
    assert "differed on every call" in messages[1]


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("request 3f2b8c1e-9a7d-4e21-b0c4-5d6e7f8a9b0c", "uuid"),
        ("at 14:03:59 today", "time of day"),
        ("epoch 1762850000", "unix timestamp"),
        ("trace 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b", "random id"),
    ],
)
def test_cache_guard_volatile_kinds(text: str, kind: str) -> None:
    assert kind in CacheGuard().check_prefix({"instructions": text})


def test_cache_guard_ignores_stable_prefixes_and_dates() -> None:
    guard = CacheGuard(min_calls=2, min_prompt_tokens=10)
    tools = [{"name": "lookup", "description": "Find an order", "input_schema": {"type": "object"}}]
    request = {"system": "Today is 2026-11-11. Be brief.", "tools": tools, "cache_control": {}}
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for read in (0, 1900, 1900):
            guard.observe(_anthropic_reply(read=read, write=1900 if not read else 0), request)
    assert guard.volatile == () and guard.stats().hit_rate > 0.5


def test_cache_guard_skips_short_prompts_and_reset_rearms() -> None:
    guard = CacheGuard(min_calls=1, min_prompt_tokens=1024, warn=False)
    guard.observe(_anthropic_reply(read=0, inp=100))
    assert guard.stats().eligible_calls == 0 and guard.stats().calls == 1
    with recorded() as events:
        guard.observe(_anthropic_reply(read=0, inp=2000))
        guard.reset()
        guard.observe(_anthropic_reply(read=0, inp=2000))
    assert [e.name for e in events].count("llm.prompt_cache_cold") == 2
    guard.observe(object())  # no usage: counted, not judged
    assert "CacheGuard" in repr(guard)


def test_cache_guard_validation() -> None:
    for bad in ({"min_calls": 0}, {"min_hit_rate": 1.0}, {"min_prompt_tokens": -1}):
        with pytest.raises(ValueError):
            CacheGuard(**bad)


def test_cache_guard_is_thread_safe() -> None:
    guard = CacheGuard(min_calls=10**9, min_prompt_tokens=0)

    def work() -> None:
        for _ in range(200):
            guard.observe(_anthropic_reply(read=1, inp=1))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    stats = guard.stats()
    assert stats.calls == 1600 and stats.cache_read_tokens == 1600


def test_guard_feeds_the_cache_guard() -> None:
    cache = CacheGuard(min_calls=1, min_prompt_tokens=0, warn=False)
    guard_anthropic(FakeClient([message(read=90, inp=10)]), cache_guard=cache).messages.create(
        model="m-1", system="x", messages=[]
    )
    assert cache.stats().hit_rate == pytest.approx(0.9)


# ================================================================== concurrency


def test_concurrent_threads_share_one_budget_with_bounded_overshoot() -> None:
    per_call = 7
    limit = 100
    threads_n = 8
    spend = Budget(tokens=limit)
    gate = threading.Barrier(threads_n)
    client = FakeClient([completion(prompt=per_call, out=0)])
    guarded = guard_openai(client, budget=spend)
    refused = []

    def worker() -> None:
        gate.wait()
        for _ in range(50):
            try:
                guarded.chat.completions.create(model="m-1", messages=[])
            except BudgetExceeded:
                refused.append(1)
                return

    threads = [threading.Thread(target=worker) for _ in range(threads_n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    spent = spend.spent().tokens
    assert limit <= spent <= limit + threads_n * per_call  # one in-flight call per thread
    assert len(refused) == threads_n


async def test_asyncio_gather_through_one_guard(fake_clock: FakeClock) -> None:
    client = AsyncFakeClient([completion(prompt=3, out=2)])
    spend = Budget(tokens=10_000)
    limiter = token_rate_limit(100_000, estimate=50)
    guarded = guard_openai(client, budget=spend, limiter=limiter)
    results = await asyncio.gather(
        *(guarded.chat.completions.create(model="m-1", messages=[]) for _ in range(40))
    )
    assert len(results) == 40 and spend.spent().tokens == 200
    assert limiter.available == pytest.approx(100_000 - 200)


async def test_asyncio_streams_in_parallel_do_not_mix_state() -> None:
    client = AsyncFakeClient([Script(anthropic_events())])
    guarded = guard_anthropic(client, budget=Budget(tokens=10_000))

    async def one() -> str:
        stream = await guarded.messages.create(model="m-1", stream=True)
        async for _ in stream:
            pass
        return stream.text

    assert await asyncio.gather(*(one() for _ in range(10))) == ["On it."] * 10


def test_disable_rate_limits_bypasses_the_limiter() -> None:
    limiter = token_rate_limit(1, estimate=1_000, block=False)
    client = FakeClient([(completion(), {"x-ratelimit-remaining-tokens": "0"}), Script([])])
    guarded = guard_openai(client, limiter=limiter)
    with disable_rate_limits():
        guarded.chat.completions.create(model="m-1")
        assert list(guarded.chat.completions.create(model="m-1", stream=True)) == []
    assert limiter.available == pytest.approx(1)
    assert client.resource.http_requests == 2


def test_json_tool_arguments_helpers() -> None:
    from python_wrap.adapters import StreamedToolCall

    assert StreamedToolCall("c", "t", '{"a": 1}', 0).input == {"a": 1}
    assert StreamedToolCall("c", "t", "[1]", 0).input == {}
    assert StreamedToolCall("c", "t", '{"a', 0).input == {}
    assert json.loads(StreamedToolCall(None, None, "", 0).arguments or "{}") == {}
