"""Two fake model providers with the call shape of a chat SDK (no network)."""

from __future__ import annotations

from typing import Any

from python_wrap import AuditTape, timeout, traced_llm
from python_wrap.llm_testing import FakeProvider, fake_llm

from .config import PRICES, REQUEST_TIMEOUT_SECONDS


class Provider:
    """One upstream: `complete` returns a response, `stream` returns an iterable of chunks."""

    def __init__(self, name: str, model: str, tape: AuditTape, fake: FakeProvider | None = None):
        self.name, self.model = name, model
        self.fake = fake or fake_llm(
            default=lambda call: f"[{model}] {call.prompt}",
            usage=(400, 600),
            model=model,
            name=name,
        )
        # Every call to this provider lands on the audit tape, with tokens and cost.
        traced = traced_llm(model=model, system=name, prices=PRICES, tape=tape)(self._complete)
        # A stalled provider must not hold a request forever (and the doctor checks for it).
        self.complete = timeout(REQUEST_TIMEOUT_SECONDS)(traced)

    def _complete(self, *, messages: list[dict[str, str]], max_tokens: int = 256) -> Any:
        return self.fake.complete(messages=messages, max_tokens=max_tokens)

    def stream(self, *, messages: list[dict[str, str]]) -> Any:
        return self.fake.stream(messages=messages)
