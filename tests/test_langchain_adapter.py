"""guard_chat_model and PythonWrapCallback against langchain-core's real base classes."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from python_wrap import events
from python_wrap.adapters.langchain import PythonWrapCallback, guard_chat_model, without_retries
from python_wrap.llm import Budget
from python_wrap.testing import fake_clock

pytestmark = pytest.mark.sdk
pytest.importorskip("langchain_core")

from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult  # noqa: E402


class RateLimited(Exception):
    status_code = 429
    response = None


class FlakyModel(BaseChatModel):
    """Fails ``failures`` times with a 429, then answers with usage_metadata."""

    max_retries: int = 2
    failures: int = 0
    calls: list[int] = []  # noqa: RUF012 - pydantic copies it per instance

    @property
    def _llm_type(self) -> str:
        return "flaky"

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any):
        self.calls.append(1)
        if len(self.calls) <= self.failures:
            raise RateLimited("slow down")
        message = AIMessage(
            content="done",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            response_metadata={"model_name": "m-1"},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any):
        self.calls.append(1)
        if len(self.calls) <= self.failures:
            raise ConnectionError("dropped before output")
        yield ChatGenerationChunk(message=AIMessageChunk(content="do"))
        yield ChatGenerationChunk(
            message=AIMessageChunk(
                content="ne",
                usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            )
        )


def test_two_429s_then_success_makes_three_calls_and_charges_once() -> None:
    model = FlakyModel(failures=2, calls=[])
    budget = Budget(tokens=1000)
    guarded = guard_chat_model(model, budget=budget, attempts=4, retry={"delay": 0})
    with fake_clock():
        reply = guarded.invoke("hello")
    assert reply.content == "done"
    assert len(guarded.model.calls) == 3
    assert guarded.model.max_retries == 0 and model.max_retries == 2
    assert budget.spent().tokens == 15


def test_ainvoke_uses_the_same_rules() -> None:
    guarded = guard_chat_model(FlakyModel(failures=1, calls=[]), retry={"delay": 0})

    async def main() -> str:
        return str((await guarded.ainvoke("hi")).content)

    with fake_clock():
        assert asyncio.run(main()) == "done"
    assert len(guarded.model.calls) == 2


def test_stream_reopens_before_output_and_charges_usage() -> None:
    budget = Budget(tokens=1000)
    guarded = guard_chat_model(FlakyModel(failures=1, calls=[]), budget=budget, retry={"delay": 0})
    with fake_clock():
        text = "".join(chunk.content for chunk in guarded.stream("hi"))
    assert text == "done"
    assert budget.spent().tokens == 15


def test_with_retry_is_refused() -> None:
    with pytest.raises(ValueError, match="with_retry"):
        without_retries(FlakyModel(calls=[]).with_retry(stop_after_attempt=3))


def test_callback_emits_genai_llm_call() -> None:
    seen: list[Any] = []
    unsubscribe = events.subscribe(
        lambda e: seen.append(e.fields) if e.name == "genai.llm_call" else None
    )
    try:
        FlakyModel(max_retries=0, calls=[]).invoke(
            "hi", config={"callbacks": [PythonWrapCallback()]}
        )
    finally:
        unsubscribe()
    assert seen and seen[0]["status"] == "ok"
    assert seen[0]["input_tokens"] == 10 and seen[0]["output_tokens"] == 5
