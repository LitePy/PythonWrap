"""A resilient LLM client against a fake provider: no SDK, no network.

Shows, end to end:

1. `llm_client` retrying a 529 "overloaded" error after the provider's Retry-After, while
   a content-policy refusal fails at once (no retry, breaker untouched);
2. a per-user dollar `Budget` refusing a user who spent their allowance;
3. `token_rate_limit` reserving an estimate and reconciling it with the real usage;
4. `fallback_models` moving to a second model when the first is down, naming the model
   that served in an ``llm.fallback_served`` event;
5. `stream_timeout` stopping a stream that stalls between chunks;
6. `llm_cache` answering a repeated question without calling the provider.

Run it with ``python examples/llm_client.py``; it asserts its own results.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from python_wrap.events import listening
from python_wrap.llm import (
    Budget,
    BudgetExceeded,
    StreamTimeout,
    fallback_models,
    llm_cache,
    stream_timeout,
    token_rate_limit,
)
from python_wrap.presets import llm_client
from python_wrap.testing import fake_clock


class ProviderError(Exception):
    """What an SDK raises: a status code, headers and a JSON body."""

    def __init__(self, status_code: int, kind: str, retry_after: str | None = None) -> None:
        super().__init__(f"{status_code} {kind}")
        self.status_code = status_code
        self.headers = {"retry-after": retry_after} if retry_after else {}
        self.body = {"type": "error", "error": {"type": kind, "code": kind}}


class FakeProvider:
    """A scripted chat endpoint returning Anthropic-style responses."""

    def __init__(self, model: str, failures: list[ProviderError] | None = None) -> None:
        self.model = model
        self.failures = list(failures or [])
        self.calls = 0

    def create(self, *, messages: list[dict[str, str]], max_tokens: int = 256) -> dict[str, Any]:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        text = messages[-1]["content"]
        return {
            "model": self.model,
            "content": f"[{self.model}] answer to {text!r}",
            "usage": {"input_tokens": 4 * len(text), "output_tokens": 50},
        }


PRICES = {"fake-large": (3.0, 15.0), "fake-small": (0.25, 1.25)}


def retries_and_refusals() -> None:
    provider = FakeProvider(
        "fake-large",
        failures=[ProviderError(529, "overloaded_error", retry_after="3")],
    )
    chat = llm_client("chat", spend=Budget(usd=1.0, prices=PRICES, name="chat-spend"))

    @chat
    def ask(question: str) -> dict[str, Any]:
        return provider.create(messages=[{"role": "user", "content": question}])

    with fake_clock() as clock:
        answer = ask("What is a circuit breaker?")
        assert clock.sleeps == [3.0], clock.sleeps  # honoured Retry-After
        provider.failures.append(ProviderError(400, "content_policy_violation"))
        try:
            ask("something the provider refuses")
        except ProviderError as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError("a refusal must not be retried")
    assert provider.calls == 3  # 529, success, refusal: the refusal ran once
    assert chat.explain().findings == ()
    print("1. retried 529 after Retry-After 3 s; refusal failed fast:", answer["content"])


def per_user_budget() -> None:
    provider = FakeProvider("fake-large")
    spend = Budget(usd=0.002, per="key", key=lambda user, question: user, prices=PRICES)

    @spend
    def ask(user: str, question: str) -> dict[str, Any]:
        return provider.create(messages=[{"role": "user", "content": question}])

    ask("ann", "hello")  # 20 in + 50 out tokens: $0.00081
    ask("ann", "hello again")  # $0.00169 total
    ask("ann", "one more")  # crosses $0.002, still answered
    try:
        ask("ann", "and another")
    except BudgetExceeded as exc:
        refused = exc
    else:
        raise AssertionError("ann should be over budget")
    ask("bob", "hello")  # bob has his own allowance
    assert provider.calls == 4
    print(f"2. ann refused after ${refused.usd:.5f} (limit ${refused.limit_usd}); bob served")


def token_limit() -> None:
    provider = FakeProvider("fake-small")
    tpm = token_rate_limit(1_000, name="fake-tpm")

    @tpm
    def ask(*, messages: list[dict[str, str]], max_tokens: int) -> dict[str, Any]:
        return provider.create(messages=messages, max_tokens=max_tokens)

    with fake_clock() as clock:
        for _ in range(5):  # estimate 305 tokens each (1525 > 1000), real usage 130
            ask(messages=[{"role": "user", "content": "x" * 20}], max_tokens=300)
    assert clock.sleeps == [], "reconciled usage leaves room for every call"
    assert 300 < tpm.available < 400
    print(f"3. 5 calls under 1000 tokens/min; {tpm.available:.0f} tokens left in the bucket")


def fallback_chain() -> None:
    large = FakeProvider("fake-large", failures=[ProviderError(503, "api_error")] * 10)
    small = FakeProvider("fake-small")

    def ask_large(question: str) -> dict[str, Any]:
        return large.create(messages=[{"role": "user", "content": question}])

    def ask_small(question: str) -> dict[str, Any]:
        return small.create(messages=[{"role": "user", "content": question}])

    ask = fallback_models([ask_large, ask_small], names=["fake-large", "fake-small"])
    served: list[str] = []
    with listening(
        lambda event: (
            served.append(event.fields["model"]) if event.name == "llm.fallback_served" else None
        )
    ):
        for _ in range(7):
            ask("status?")
    assert served == ["fake-small"] * 7
    assert large.calls == 5  # its breaker opened after 5 failures: later calls skip it
    print("4. large model down; small model served 7 calls; large tried only 5 times")


def stalled_stream() -> None:
    def provider_stream() -> Iterator[str]:
        yield "Circuit "
        yield "breakers "
        time.sleep(0.3)  # the connection stalls
        yield "stop retry storms."

    received: list[str] = []
    try:
        for chunk in stream_timeout(provider_stream(), first_token=2, chunk_gap=0.05):
            received.append(chunk)
    except StreamTimeout as exc:
        tripped = exc.limit
    else:
        raise AssertionError("the stall should trip chunk_gap")
    assert received == ["Circuit ", "breakers "] and tripped == "chunk_gap"
    print(f"5. stream stopped by {tripped} after {''.join(received)!r}")


def cached_answers() -> None:
    provider = FakeProvider("fake-small")

    @llm_cache(ttl=600)
    def ask(*, model: str, messages: list[dict[str, str]], temperature: float = 0.0) -> Any:
        return provider.create(messages=messages)

    question = [{"role": "user", "content": "Define idempotent."}]
    first = ask(model="fake-small", messages=question)
    again = ask(model="fake-small", messages=question, temperature=0.0)
    assert first == again and provider.calls == 1
    print("6. repeated question served from the cache; provider called once")


if __name__ == "__main__":
    retries_and_refusals()
    per_user_budget()
    token_limit()
    fallback_chain()
    stalled_stream()
    cached_answers()
