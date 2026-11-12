"""guard_openai / guard_anthropic end to end, offline: one retry loop, budgets, safe streams.

The two "clients" below are tiny fakes with the shape of the official SDK clients
(``max_retries``, ``with_options``, ``chat.completions.create``, ``messages.create``,
``with_raw_response``, stream events). With the real packages you would pass
``OpenAI()`` / ``Anthropic()`` instead; nothing else changes. No SDK, no network, no key.

What it shows:

1. A 429 is retried by python-wrap only: the guarded copy has ``max_retries=0``, so the
   fake's SDK-style retry loop never multiplies the attempts.
2. The token limiter follows ``x-ratelimit-remaining-tokens`` from the raw response.
3. A stream that drops before any output is reopened invisibly; one that drops in the
   middle of a tool call raises ``StreamInterrupted`` with the partial text and the
   pending call, and is never replayed.
4. ``CacheGuard`` spots a request id in the system prompt that defeats prompt caching.

Run it: ``python examples/sdk_adapters/guarded_clients.py``
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

from python_wrap import CacheGuard, StreamInterrupted, guard_anthropic, guard_openai
from python_wrap.adapters import PromptCacheWarning
from python_wrap.llm import Budget, token_rate_limit
from python_wrap.testing import fake_clock

# --------------------------------------------------------------------- the fakes


class RateLimitError(Exception):
    """Shaped like ``openai.RateLimitError``: a status code and response headers."""

    status_code = 429

    def __init__(self) -> None:
        super().__init__("Rate limit reached")
        self.response = SimpleNamespace(status_code=429, headers={})


class Raw:
    """What ``with_raw_response.create`` returns: headers plus ``parse()``."""

    def __init__(self, parsed: Any, headers: dict[str, str]) -> None:
        self.headers, self._parsed = headers, parsed

    def parse(self) -> Any:
        return self._parsed


class Endpoint:
    """One SDK resource that retries by itself ``client.max_retries`` times, like the SDKs."""

    def __init__(self, client: FakeSDK, replies: list[Any]) -> None:
        self.client, self.replies, self.http_requests = client, replies, [0]
        self.with_raw_response = SimpleNamespace(create=self._raw)

    def _raw(self, **kwargs: Any) -> Raw:
        for attempt in range(1 + self.client.max_retries):
            self.http_requests[0] += 1
            reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
            if callable(reply):
                reply = reply()
            if isinstance(reply, Exception):
                if attempt < self.client.max_retries:
                    continue  # the SDK's own retry loop
                raise reply
            return Raw(reply, {"x-ratelimit-remaining-tokens": "20000"})
        raise AssertionError("unreachable")

    def create(self, **kwargs: Any) -> Any:
        return self._raw(**kwargs).parse()


class FakeSDK:
    """Has the attributes the guards use on ``OpenAI()`` and ``Anthropic()``."""

    def __init__(self, replies: list[Any], max_retries: int = 2) -> None:
        self.max_retries = max_retries
        self._endpoint = Endpoint(self, replies)
        self.chat = SimpleNamespace(completions=self._endpoint)
        self.messages = self._endpoint

    def with_options(self, *, max_retries: int) -> FakeSDK:
        clone = FakeSDK([], max_retries)
        clone._endpoint.replies = self._endpoint.replies
        clone._endpoint.http_requests = self._endpoint.http_requests
        return clone

    @property
    def http_requests(self) -> int:
        return self._endpoint.http_requests[0]


def chat_completion(text: str, prompt: int, completion: int) -> Any:
    message = SimpleNamespace(role="assistant", content=text, tool_calls=None)
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
    return SimpleNamespace(
        model="model-a",
        choices=[SimpleNamespace(index=0, message=message, finish_reason="stop")],
        usage=usage,
    )


def anthropic_usage(inp: int, out: int, read: int = 0, write: int = 0) -> Any:
    return SimpleNamespace(
        input_tokens=inp,
        output_tokens=out,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=write,
    )


def event(kind: str, **fields: Any) -> Any:
    return SimpleNamespace(type=kind, **fields)


def refund_events() -> list[Any]:
    """Anthropic stream events: some text, then a refund tool call."""
    block = SimpleNamespace(type="tool_use", id="toolu_7", name="refund", input={})
    text_block = SimpleNamespace(type="text", text="")
    return [
        event(
            "message_start", message=SimpleNamespace(model="model-b", usage=anthropic_usage(80, 1))
        ),
        event("content_block_start", index=0, content_block=text_block),
        event(
            "content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text="Refunding now."),
        ),
        event("content_block_stop", index=0),
        event("content_block_start", index=1, content_block=block),
        event(
            "content_block_delta",
            index=1,
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"order": "A-'),
        ),
        event(
            "content_block_delta",
            index=1,
            delta=SimpleNamespace(type="input_json_delta", partial_json='17"}'),
        ),
        event("content_block_stop", index=1),
        event("message_delta", usage=SimpleNamespace(output_tokens=20)),
        event("message_stop"),
    ]


def dropping_stream(events: list[Any], after: int) -> Any:
    """A stream that loses its connection after ``after`` events."""

    def iterate() -> Iterator[Any]:
        yield from events[:after]
        raise ConnectionResetError("connection reset by peer")

    return iterate


# --------------------------------------------------------------------- the demo


def retries_and_headers() -> dict[str, Any]:
    sdk = FakeSDK([RateLimitError(), chat_completion("Order A-17 has shipped.", 120, 30)])
    spend = Budget(tokens=50_000, name="openai-daily")
    limiter = token_rate_limit(100_000, name="openai-tpm")
    client = guard_openai(
        sdk, budget=spend, limiter=limiter, attempts=3, retry={"delay": 1.0, "jitter": 0}
    )
    with fake_clock() as clock:
        reply = client.chat.completions.create(
            model="model-a", messages=[{"role": "user", "content": "Where is A-17?"}]
        )
        available = limiter.available  # read under the fake clock
    return {
        "answer": reply.choices[0].message.content,
        "http_requests": sdk.http_requests,
        "waited": clock.sleeps,
        "spent": spend.spent().tokens,
        "limiter": available,
        "sdk_retries": (client.raw.max_retries, client.client.max_retries),
    }


def streams() -> dict[str, Any]:
    events = refund_events()
    opens = [dropping_stream(events, after=1), lambda: iter(events)]  # drops after message_start
    sdk = FakeSDK([lambda: opens.pop(0)()])
    spend = Budget(tokens=10_000, name="anthropic-daily")
    client = guard_anthropic(sdk, budget=spend, retry={"delay": 0.5})
    with fake_clock():
        stream = client.messages.create(model="model-b", max_tokens=300, messages=[], stream=True)
        received = list(stream)
    resumed = {
        "attempts": stream.attempts,
        "events": len(received),
        "text": stream.text,
        "tool_calls": [(call.name, call.input) for call in stream.tool_calls],
        "spent": spend.spent().tokens,
    }

    broken = FakeSDK([dropping_stream(events, after=6)])
    client = guard_anthropic(broken)
    stream = client.messages.create(model="model-b", max_tokens=300, messages=[], stream=True)
    try:
        for _ in stream:
            pass
    except StreamInterrupted as exc:
        interrupted = {
            "text": exc.text,
            "complete": exc.tool_calls,
            "pending": [(call.name, call.arguments) for call in exc.pending],
            "requests": broken.http_requests,
        }
    else:
        raise AssertionError("the stream should have been interrupted")
    return {"resumed": resumed, "interrupted": interrupted}


def prompt_cache() -> dict[str, Any]:
    cold = SimpleNamespace(model="model-b", content=[], usage=anthropic_usage(40, 10, 0, 2_500))
    sdk = FakeSDK([cold])
    cache = CacheGuard(min_calls=3, name="support-cache")
    client = guard_anthropic(sdk, cache_guard=cache)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for request_id in (
            "9f1c2e3d-0a4b-4c5d-8e6f-7a8b9c0d1e2f",
            "1b2c3d4e-5f60-4a7b-8c9d-0e1f2a3b4c5d",
            "aa0b1c2d-3e4f-4a5b-9c6d-7e8f9a0b1c2d",
        ):
            client.messages.create(
                model="model-b",
                max_tokens=50,
                system=f"Request {request_id}. You answer billing questions.",
                messages=[{"role": "user", "content": "Why was I charged twice?"}],
            )
    return {
        "warnings": [str(w.message) for w in caught if w.category is PromptCacheWarning],
        "volatile": cache.volatile,
        "hit_rate": cache.stats().hit_rate,
    }


def main() -> None:
    first = retries_and_headers()
    print(f"1. answer={first['answer']!r} after {first['http_requests']} HTTP requests")
    print(f"   waited {first['waited']} s (backoff), spent {first['spent']} tokens")
    original, copy = first["sdk_retries"]
    print(f"   SDK max_retries: original client={original}, guarded copy={copy}")
    print(f"   limiter lowered to the provider's remaining tokens: {first['limiter']:.0f}")
    assert first["http_requests"] == 2  # not 2 x 3: only python-wrap retried
    assert first["waited"] == [1.0]
    assert first["spent"] == 150
    assert first["sdk_retries"] == (2, 0)
    assert first["limiter"] == 20_000

    second = streams()
    resumed, interrupted = second["resumed"], second["interrupted"]
    print(
        f"2. stream reopened after an early drop: attempts={resumed['attempts']}, "
        f"events={resumed['events']}, text={resumed['text']!r}, tools={resumed['tool_calls']}"
    )
    print(
        f"   stream dropped mid tool call: text={interrupted['text']!r}, "
        f"pending={interrupted['pending']}, requests={interrupted['requests']}"
    )
    assert resumed["attempts"] == 2 and resumed["events"] == len(refund_events())
    assert resumed["tool_calls"] == [("refund", {"order": "A-17"})]
    assert resumed["spent"] == 100
    assert interrupted["text"] == "Refunding now." and interrupted["complete"] == ()
    assert interrupted["pending"] == [("refund", '{"order": "A-')]
    assert interrupted["requests"] == 1  # never replayed

    third = prompt_cache()
    print(f"3. prompt cache: hit rate {third['hit_rate']:.0%}, volatile={third['volatile']}")
    for message in third["warnings"]:
        print(f"   warning: {message}")
    assert third["volatile"] == ("uuid",)
    assert third["hit_rate"] == 0.0
    assert len(third["warnings"]) == 2
    print("ok")


if __name__ == "__main__":
    main()
