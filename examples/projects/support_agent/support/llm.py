"""The model client: spend budget, retries, circuit breaker, timeout and a backup model."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from python_wrap import AuditTape, Redactor, fallback_models, traced_llm
from python_wrap.llm import Budget
from python_wrap.presets import llm_client
from support.config import Settings


def make_budget(settings: Settings) -> Budget:
    """Tokens one agent run may spend. ``per="run"`` keeps a separate tally per run id."""
    return Budget(tokens=settings.token_budget, per="run", name="support-spend")


def make_ask(
    settings: Settings,
    *,
    primary: Any,
    backup: Any,
    budget: Budget,
    tape: AuditTape,
    redactor: Redactor,
) -> Callable[[list[dict[str, Any]]], Any]:
    """Build ``ask(messages)``.

    Layers, outermost first: the ``llm_client`` policy (spend budget, circuit breaker,
    retries that honour ``Retry-After``, per-attempt timeout), then ``traced_llm`` (one
    redacted tape record per logical call), then the fallback chain (a backup model when
    the primary is rate limited or overloaded).
    """

    def call_primary(messages: list[dict[str, Any]]) -> Any:
        return primary.complete(model="support-large", messages=messages)

    def call_backup(messages: list[dict[str, Any]]) -> Any:
        return backup.complete(model="support-small", messages=messages)

    chain = fallback_models(
        [call_primary, call_backup], names=["support-large", "support-small"], name="support-llm"
    )
    policy = llm_client(
        "support-llm",
        spend=budget,
        attempts=settings.llm_attempts,
        timeout_seconds=settings.llm_timeout_seconds,
    )

    @policy
    @traced_llm(model="support-llm", system="demo", tape=tape, redactor=redactor)
    def ask(messages: list[dict[str, Any]]) -> Any:
        return chain(messages)

    return ask  # type: ignore[no-any-return]
