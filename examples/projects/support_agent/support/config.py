"""Settings for one agent deployment."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    max_steps: int = 10  # LLM calls plus tool calls in one run
    max_repeats: int = 2  # the same tool with the same arguments, at most this often
    token_budget: int = 2000  # tokens one run may spend
    refund_limit_cents: int = 5000  # refunds above this need a human
    llm_timeout_seconds: float = 20.0
    llm_attempts: int = 3
    tape_path: str = "audit.jsonl"
