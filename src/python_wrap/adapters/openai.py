"""Guard an OpenAI Python SDK client: one retry loop, a budget, a token limit, safe streams.

`guard_openai` wraps an ``openai.OpenAI`` or ``openai.AsyncOpenAI`` client (or anything
shaped like one) in a proxy with the same call surface:

- ``guarded.chat.completions.create(...)``
- ``guarded.responses.create(...)``

Each call goes through tracing (optional), the `Budget`, python-wrap's retry loop (the
SDK's own retries are switched off with `no_sdk_retries`, so attempts never multiply),
your per-attempt ``policy`` and the `TokenRateLimiter`, which is lowered to the
``x-ratelimit-remaining-tokens`` header read through ``with_raw_response``. With
``stream=True`` the result is a `ResumableStream`: reopened when it fails before any
output, `StreamInterrupted` after. Chat streams get
``stream_options={"include_usage": True}`` (unless you pass ``stream_options``) so
their usage can be charged. Every other attribute (``models``, ``embeddings``,
``files``...) is the original client's, unguarded.

The ``openai`` package is never imported here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from python_wrap.adapters._sdk import CacheGuard, GuardedClient, _Endpoint, _Namespace
from python_wrap.llm import Budget, TokenRateLimiter

__all__ = ["GuardedOpenAI", "guard_openai"]


class GuardedOpenAI(GuardedClient):
    """The proxy `guard_openai` returns; see `GuardedClient` for the layers.

    Attributes:
        chat: ``chat.completions.create`` is guarded; other attributes pass through.
        responses: ``responses.create`` is guarded; ``retrieve``/``delete``... pass
            through.
    """

    def __init__(self, client: Any, **options: Any) -> None:
        super().__init__(client, system="openai", **options)
        self.chat = _Namespace(
            self, ("chat",), completions=_Endpoint(self, ("chat", "completions"), "chat")
        )
        self.responses = _Endpoint(self, ("responses",), "responses")

    def _primary(self) -> _Endpoint:
        return self.chat.completions  # type: ignore[no-any-return]


def guard_openai(
    client: Any,
    *,
    budget: Budget | None = None,
    limiter: TokenRateLimiter | None = None,
    policy: Callable[[Callable[..., Any]], Callable[..., Any]] | None = None,
    attempts: int = 4,
    retry: Mapping[str, Any] | None = None,
    owns_retries: bool = True,
    trace: bool | Mapping[str, Any] = False,
    cache_guard: CacheGuard | None = None,
    read_headers: bool = True,
    first_token: float | None = None,
    chunk_gap: float | None = None,
    total: float | None = None,
    is_async: bool | None = None,
) -> GuardedOpenAI:
    """Wrap an OpenAI SDK client so every call is retried, budgeted and rate limited once.

    Args:
        client: ``OpenAI()`` / ``AsyncOpenAI()`` (or a look-alike).
        budget: Checked before each call and charged with ``response.usage`` after.
        limiter: Reserves an estimate per attempt, settles with the real usage, and is
            lowered to the provider's ``x-ratelimit-remaining-tokens``; a 429's
            ``retry-after`` drains it.
        policy: A per-attempt decorator (a `Policy` with a timeout or breaker). Do not
            put a retry in it: retries belong to ``attempts``.
        attempts: Total attempts per call (python-wrap's loop, `llm_retry`).
        retry: Extra `llm_retry` options (``delay``, ``max_delay``, ``max_time``,
            ``budget``, ``retry_if``); ``delay``/``max_delay``/``retry_if`` also apply
            to reopening streams.
        owns_retries: True (default): python-wrap retries and the client is copied with
            ``max_retries=0``. False: the SDK keeps its retries and python-wrap makes a
            single attempt. Either way exactly one layer retries.
        trace: True (or `traced_llm` options) to publish a ``genai.llm_call`` event per
            call.
        cache_guard: A `CacheGuard` fed every response and request.
        read_headers: Use ``with_raw_response`` to read rate-limit headers.
        first_token: Stall limit before a stream's first event (reopened while safe).
        chunk_gap: Stall limit between stream events.
        total: Limit for a whole stream.
        is_async: Force sync/async; detected from the client class name otherwise.

    Returns:
        A `GuardedOpenAI`.

    Example:
        ```python
        from types import SimpleNamespace

        from python_wrap.adapters import guard_openai
        from python_wrap.llm import Budget
        from python_wrap.testing import fake_clock


        class RateLimitError(Exception):
            status_code = 429


        class Completions:
            def __init__(self) -> None:
                self.calls = 0

            def create(self, **kwargs: object) -> SimpleNamespace:
                self.calls += 1
                if self.calls == 1:
                    raise RateLimitError
                usage = SimpleNamespace(prompt_tokens=12, completion_tokens=3)
                return SimpleNamespace(model="m-1", usage=usage, choices=[])


        class Client:  # the shape of openai.OpenAI
            max_retries = 2

            def __init__(self, completions: Completions | None = None) -> None:
                self.chat = SimpleNamespace(completions=completions or Completions())

            def with_options(self, *, max_retries: int) -> "Client":
                clone = Client(self.chat.completions)
                clone.max_retries = max_retries
                return clone


        spend = Budget(tokens=1_000)
        client = guard_openai(Client(), budget=spend)
        with fake_clock():
            client.chat.completions.create(model="m-1", messages=[])
        assert client.client.max_retries == 0  # only python-wrap retries
        assert spend.spent().tokens == 15
        ```
    """
    return GuardedOpenAI(
        client,
        budget=budget,
        limiter=limiter,
        policy=policy,
        attempts=attempts,
        retry=retry,
        owns_retries=owns_retries,
        trace=trace,
        cache_guard=cache_guard,
        read_headers=read_headers,
        first_token=first_token,
        chunk_gap=chunk_gap,
        total=total,
        is_async=is_async,
    )
