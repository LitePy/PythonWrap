"""Guard an Anthropic Python SDK client: one retry loop, a budget, a token limit, safe streams.

`guard_anthropic` wraps an ``anthropic.Anthropic`` or ``anthropic.AsyncAnthropic``
client (or anything shaped like one) in a proxy with the same call surface:

- ``guarded.messages.create(...)``, including ``stream=True``;
- ``with guarded.messages.stream(...) as stream:`` (``async with`` for async clients);
- ``guarded.beta.messages.tool_runner(...)``: each step is budgeted, rate limited and
  fed to the `CacheGuard`.

Calls go through the same layers as `guard_openai`; the limiter follows
``anthropic-ratelimit-tokens-remaining`` / ``anthropic-ratelimit-input-tokens-remaining``.
The tool runner is the exception for retries: a step runs your tools before the next
request, so python-wrap never repeats a step; the runner uses the original client,
whose SDK retries repeat only the HTTP request.

The ``anthropic`` package is never imported here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from python_wrap.adapters._sdk import CacheGuard, GuardedClient, _Endpoint, _Namespace, _Runner
from python_wrap.llm import Budget, TokenRateLimiter

__all__ = ["GuardedAnthropic", "guard_anthropic"]


class _BetaMessages:
    """``guarded.beta.messages``: ``tool_runner`` is accounted; the rest passes through."""

    def __init__(self, guard: GuardedAnthropic) -> None:
        self._guard = guard

    def tool_runner(self, *args: Any, **kwargs: Any) -> _Runner:
        """Start the SDK's tool runner; every step is budgeted and rate limited."""
        runner = self._guard.raw.beta.messages.tool_runner(*args, **kwargs)
        return _Runner(self._guard, runner, kwargs, "anthropic.beta.messages.tool_runner")

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._guard.raw.beta.messages, name)


class GuardedAnthropic(GuardedClient):
    """The proxy `guard_anthropic` returns; see `GuardedClient` for the layers.

    Attributes:
        messages: ``create`` and ``stream`` are guarded; ``count_tokens``... pass
            through.
        beta: ``beta.messages.tool_runner`` is accounted per step.
    """

    def __init__(self, client: Any, **options: Any) -> None:
        super().__init__(client, system="anthropic", **options)
        self.messages = _Endpoint(self, ("messages",), "messages")
        self.beta = _Namespace(self, ("beta",), messages=_BetaMessages(self))

    def _primary(self) -> _Endpoint:
        return self.messages


def guard_anthropic(
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
) -> GuardedAnthropic:
    """Wrap an Anthropic SDK client so every call is retried, budgeted and rate limited once.

    The arguments are those of `guard_openai`. Usage is read from ``response.usage``
    (``input_tokens``, ``output_tokens`` and the cache read/creation counts); streams
    add ``message_start`` and ``message_delta`` usage.

    Returns:
        A `GuardedAnthropic`.

    Example:
        ```python
        from types import SimpleNamespace

        from python_wrap.adapters import guard_anthropic
        from python_wrap.llm import Budget


        class Messages:
            def create(self, **kwargs: object) -> SimpleNamespace:
                usage = SimpleNamespace(input_tokens=20, output_tokens=5,
                                        cache_read_input_tokens=0,
                                        cache_creation_input_tokens=0)
                return SimpleNamespace(model="m-1", usage=usage, content=[])


        class Client:  # the shape of anthropic.Anthropic
            max_retries = 2
            messages = Messages()

            def with_options(self, *, max_retries: int) -> "Client":
                clone = Client()
                clone.max_retries = max_retries
                return clone


        spend = Budget(tokens=100)
        client = guard_anthropic(Client(), budget=spend)
        client.messages.create(model="m-1", max_tokens=10, messages=[])
        assert spend.spent().tokens == 25
        ```
    """
    return GuardedAnthropic(
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
