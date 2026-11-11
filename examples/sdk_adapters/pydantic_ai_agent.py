"""python-wrap around a pydantic-ai ``Agent``: safe tools and a run guard.

Targets ``pydantic-ai>=1.0,<2``: ``Agent(model, system_prompt=...)``, ``agent.tool_plain(fn)``
to register a plain function as a tool (the signature and docstring become the tool schema, so
the python-wrap wrappers keep both), and ``agent.run_sync(prompt)`` whose result has
``.output``. python-wrap never imports pydantic-ai; the tools are ordinary functions.

What is added around the agent:

* tools wrapped with ``ToolRegistry.tool``: a retried read tool, a replay-safe write tool
  (one note per ticket however often the model asks), and a ``Quota`` on a paid search;
* a ``RunGuard`` (steps, repeated calls, wall time) around ``agent.run_sync``.

Run it:

* ``PYDANTIC_AI_MODEL=<provider:model> <PROVIDER>_API_KEY=... \
  python examples/sdk_adapters/pydantic_ai_agent.py``
* ``python examples/sdk_adapters/pydantic_ai_agent.py --offline`` uses a tiny fake agent with
  the same surface (no SDK, no network). Without a model and key, or without the SDK, the
  script prints a SKIP notice and exits 0.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from python_wrap import QuotaExhausted, RunGuard, ToolRegistry, retry
from python_wrap.quotas import Quota

NOTES: dict[str, str] = {}
SEARCHES: list[str] = []


def build_tools(searches: int = 3) -> ToolRegistry:
    tools = ToolRegistry(guard=RunGuard(max_steps=10, max_repeats=3, max_seconds=60))
    paid = Quota(searches, "paid-search")

    @tools.tool(read_only=True, retry=retry(max_attempts=3, delay=0.1, on=ConnectionError))
    def search_docs(query: str) -> str:
        """Search the internal documentation (a paid API: limited per run)."""
        try:
            paid.take()
        except QuotaExhausted:
            return "search allowance used up: answer from what you already know"
        SEARCHES.append(query)
        return f"doc hit for {query!r}"

    @tools.tool(idempotent_key=lambda ticket_id, note: ticket_id)
    def add_note(ticket_id: str, note: str) -> str:
        """Attach a note to a ticket."""
        NOTES[ticket_id] = note
        return f"noted {ticket_id}"

    return tools


def register(agent: Any, tools: ToolRegistry) -> None:
    """Hand every wrapped tool to the agent; names, signatures and docstrings carry over."""
    for name in tools:
        agent.tool_plain(tools.get(name))


def run_agent(agent: Any, tools: ToolRegistry, prompt: str) -> str:
    register(agent, tools)
    with tools.run():  # the guard sees every tool step of this run
        return str(agent.run_sync(prompt).output)


# --------------------------------------------------------------------- offline fake


class FakeAgent:
    """The slice of ``pydantic_ai.Agent`` used above, driven by a script of tool calls."""

    def __init__(self, script: list[tuple[str, dict[str, Any]]]) -> None:
        self.script = script
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool_plain(self, func: Callable[..., Any]) -> Callable[..., Any]:
        self.tools[func.__name__] = func
        return func

    def run_sync(self, prompt: str) -> Any:
        results = [self.tools[name](**args) for name, args in self.script]
        return SimpleNamespace(output=f"done after {len(results)} tool calls: {results[-1]}")


def run_offline(script: list[tuple[str, dict[str, Any]]] | None = None) -> str:
    NOTES.clear()
    SEARCHES.clear()
    script = script or [
        ("search_docs", {"query": "reset password"}),
        ("add_note", {"ticket_id": "T-1", "note": "sent the reset guide"}),
        ("add_note", {"ticket_id": "T-1", "note": "sent the reset guide"}),  # a model repeat
    ]
    return run_agent(FakeAgent(script), build_tools(), "Help with T-1")


# --------------------------------------------------------------------- entry point

_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--offline" in args:
        print(run_offline())
        return 0
    model = os.environ.get("PYDANTIC_AI_MODEL")
    key_name = _KEYS.get((model or "").split(":")[0])
    if not model or (key_name and not os.environ.get(key_name)):
        print(
            "SKIP: set PYDANTIC_AI_MODEL (provider:model) and the provider's API key (or --offline)"
        )
        return 0
    try:
        from pydantic_ai import Agent  # imported lazily: only needed for the real call
    except ImportError:
        print("SKIP: the pydantic-ai package is not installed (pip install pydantic-ai)")
        return 0
    agent = Agent(model, system_prompt="You are a support assistant. Use the tools.")
    print(run_agent(agent, build_tools(), "Help with ticket T-1: a customer cannot log in."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
