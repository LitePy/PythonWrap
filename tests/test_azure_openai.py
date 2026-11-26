"""guard_openai with the real AzureOpenAI / AsyncAzureOpenAI clients over a mock transport."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from python_wrap.adapters import guard_openai, sdk_max_retries
from python_wrap.llm import Budget, TokenRateLimiter
from python_wrap.testing import fake_clock

pytestmark = pytest.mark.sdk
openai = pytest.importorskip("openai")
httpx = pytest.importorskip("httpx")

EMBEDDING = {
    "object": "list",
    "model": "text-embedding-3-small",
    "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
    "usage": {"prompt_tokens": 7, "total_tokens": 7},
}
FILTERED = {
    "error": {
        "message": "The response was filtered",
        "type": None,
        "param": "prompt",
        "code": "content_filter",
    }
}


def handler(seen: list[str]) -> Any:
    def respond(request: Any) -> Any:
        seen.append(request.url.path)
        headers = {"x-ratelimit-remaining-tokens": "5000"}
        if request.url.path.endswith("/embeddings"):
            return httpx.Response(200, json=EMBEDDING, headers=headers)
        return httpx.Response(400, json=FILTERED, headers=headers)

    return respond


def azure(seen: list[str], *, is_async: bool = False) -> Any:
    options = {
        "api_key": "test",
        "api_version": "2024-06-01",
        "azure_endpoint": "https://example-resource.invalid",
    }
    if is_async:
        transport = httpx.MockTransport(handler(seen))
        return openai.AsyncAzureOpenAI(
            http_client=httpx.AsyncClient(transport=transport), **options
        )
    transport = httpx.MockTransport(handler(seen))
    return openai.AzureOpenAI(http_client=httpx.Client(transport=transport), **options)


def test_embeddings_are_charged_and_content_filter_is_not_retried() -> None:
    seen: list[str] = []
    budget = Budget(tokens=1000)
    guarded = guard_openai(azure(seen), budget=budget, attempts=4, retry={"delay": 0})
    assert sdk_max_retries(guarded.client) == 0
    for _ in range(2):
        guarded.embeddings.create(model="embed-deployment", input=["a document"])
    assert budget.spent().tokens == 14  # input tokens only, charged twice
    with fake_clock(), pytest.raises(openai.BadRequestError):
        guarded.chat.completions.create(
            model="chat-deployment", messages=[{"role": "user", "content": "x"}]
        )
    assert sum(path.endswith("/chat/completions") for path in seen) == 1


def test_async_azure_client_is_detected_and_guarded() -> None:
    seen: list[str] = []
    guarded = guard_openai(azure(seen, is_async=True))
    assert guarded.is_async

    async def main() -> Any:
        return await guarded.embeddings.create(model="embed-deployment", input="hi")

    assert asyncio.run(main()).usage.prompt_tokens == 7


def test_limiter_key_keeps_one_window_per_deployment() -> None:
    seen: list[str] = []
    limiter = TokenRateLimiter(tokens_per_minute=10_000)
    guarded = guard_openai(azure(seen), limiter=limiter, limiter_key=lambda kw: kw["model"])
    guarded.embeddings.create(model="east", input="a")
    guarded.embeddings.create(model="west", input="b")
    assert set(guarded._limiters) == {"east", "west"}
    assert guarded._limiters["east"] is not guarded._limiters["west"]
    with pytest.raises(ValueError, match="limiter_key"):
        guard_openai(
            azure(seen),
            limiter=TokenRateLimiter(tokens_per_minute=10, store=object()),
            limiter_key=lambda kw: kw["model"],
        )
