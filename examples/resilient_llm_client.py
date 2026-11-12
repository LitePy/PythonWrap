"""A resilient LLM client built from python-wrap pieces, against a fake provider.

No SDK and no network: ``fake_llm`` plays the model. The script shows:

1. the ``llm_client`` preset retrying a rate limit after the provider's ``Retry-After``;
2. a ``Budget`` refusing calls once the token allowance is spent;
3. ``fallback_models`` serving from a backup model while the primary is overloaded;
4. ``llm_cache`` answering a repeated question without calling the provider.

Run it with ``python examples/resilient_llm_client.py``; it asserts its own results.
"""

from __future__ import annotations

from typing import Any

from python_wrap import fake_llm
from python_wrap.llm import Budget, BudgetExceeded, fallback_models, llm_cache
from python_wrap.llm_testing import OverloadedError, RateLimitError
from python_wrap.presets import llm_client
from python_wrap.testing import fake_clock


def ask_with(provider: Any, question: str) -> Any:
    return provider.complete(messages=[{"role": "user", "content": question}])


def retries_and_budget() -> None:
    provider = fake_llm([RateLimitError("slow down", status_code=429, retry_after=2.0), "hi"])
    spend = Budget(tokens=60, name="chat-spend")
    chat = llm_client("chat", spend=spend)

    @chat
    def ask(question: str) -> Any:
        return ask_with(provider, question)

    with fake_clock() as clock:
        answer = ask("Say hi")
        assert clock.sleeps == [2.0], clock.sleeps  # the provider's Retry-After
    assert answer.text == "hi" and provider.call_count == 2
    assert spend.spent().tokens > 0
    print("1. retried a 429 after Retry-After 2 s:", answer.text)

    tight = Budget(tokens=10, name="tight")
    cheap = fake_llm(["ok"], default="ok", usage=(8, 8))

    @tight
    def ask_cheap(question: str) -> Any:
        return ask_with(cheap, question)

    ask_cheap("one")  # 16 tokens: crosses the limit, answer still returned
    try:
        ask_cheap("two")
    except BudgetExceeded as exc:
        assert exc.scope == "total"
    else:
        raise AssertionError("the second call should be refused")
    assert cheap.call_count == 1
    print("2. budget refused the call after the allowance was spent")


def model_fallback() -> None:
    primary = fake_llm([OverloadedError("busy")], cycle=True)
    backup = fake_llm(["from backup"], default="from backup")

    ask = fallback_models(
        [lambda q: ask_with(primary, q), lambda q: ask_with(backup, q)],
        names=["primary", "backup"],
    )
    assert ask("status?").text == "from backup"
    assert primary.call_count == 1 and backup.call_count == 1
    print("3. primary overloaded; backup model served the answer")


def cached() -> None:
    provider = fake_llm(["a definition"], default="a definition")

    @llm_cache(ttl=60)
    def ask(*, model: str, messages: list[dict[str, str]]) -> Any:
        return provider.complete(messages=messages)

    messages = [{"role": "user", "content": "Define idempotent."}]
    first = ask(model="m-1", messages=messages)
    again = ask(model="m-1", messages=messages)
    assert first.text == again.text and provider.call_count == 1
    print("4. repeated question served from the cache; provider called once")


if __name__ == "__main__":
    retries_and_budget()
    model_fallback()
    cached()
