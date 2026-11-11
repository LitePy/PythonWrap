"""Story: an LLM client rides out rate limits, respects a budget, falls back and caches."""

from __future__ import annotations

from typing import Any

import pytest

from python_wrap import fake_llm
from python_wrap.chaos import chaos
from python_wrap.llm import Budget, BudgetExceeded, fallback_models, llm_cache
from python_wrap.llm_testing import OverloadedError, RateLimitError
from python_wrap.presets import llm_client
from python_wrap.testing import FakeClock

pytestmark = pytest.mark.e2e


def ask_with(provider: Any, question: str) -> Any:
    return provider.complete(messages=[{"role": "user", "content": question}])


def test_rate_limit_is_retried_after_the_providers_retry_after(fake_clock: FakeClock) -> None:
    provider = fake_llm([RateLimitError("slow down", status_code=429, retry_after=2.0), "hi"])
    spend = Budget(tokens=1_000, name="chat-spend")

    @llm_client("chat", spend=spend)
    def ask(question: str) -> Any:
        return ask_with(provider, question)

    answer = ask("Say hi")
    assert answer.text == "hi"
    assert provider.call_count == 2
    assert fake_clock.sleeps == [2.0]
    assert spend.spent().tokens > 0


def test_budget_refuses_calls_once_spent() -> None:
    tight = Budget(tokens=10, name="tight")
    provider = fake_llm(["ok"], default="ok", usage=(8, 8))

    @tight
    def ask(question: str) -> Any:
        return ask_with(provider, question)

    assert ask("one").text == "ok"  # crosses the limit; the answer is still returned
    with pytest.raises(BudgetExceeded) as info:
        ask("two")
    assert info.value.scope == "total"
    assert provider.call_count == 1


def test_overloaded_primary_falls_back_to_the_backup() -> None:
    primary = fake_llm([OverloadedError("busy")], cycle=True)
    backup = fake_llm(["from backup"], default="from backup")
    ask = fallback_models(
        [lambda q: ask_with(primary, q), lambda q: ask_with(backup, q)],
        names=["primary", "backup"],
    )
    assert ask("status?").text == "from backup"
    assert (primary.call_count, backup.call_count) == (1, 1)


def test_repeated_question_is_served_from_the_cache() -> None:
    provider = fake_llm(["a definition"], default="a definition")

    @llm_cache(ttl=60)
    def ask(*, model: str, messages: list[dict[str, str]]) -> Any:
        return provider.complete(messages=messages)

    messages = [{"role": "user", "content": "Define idempotent."}]
    assert ask(model="m-1", messages=messages).text == "a definition"
    assert ask(model="m-1", messages=messages).text == "a definition"
    assert provider.call_count == 1


def test_client_survives_injected_rate_limit_chaos(fake_clock: FakeClock) -> None:
    provider = fake_llm(["fine"], default="fine")

    @llm_client("chaotic")
    def ask(question: str) -> Any:
        return ask_with(provider, question)

    chaos.llm.rate_limit("llm.*", times=2, retry_after=3)
    try:
        assert ask("hello").text == "fine"
    finally:
        chaos.clear()
    assert fake_clock.sleeps == [3.0, 3.0]
    assert provider.served_count == 1
