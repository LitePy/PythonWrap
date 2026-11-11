"""The agent loop: ask the model, run the tools it requests, repeat until it answers.

The loop knows nothing about retries, budgets or tracing. It receives two callables:
``ask(messages)`` and ``dispatch(name, arguments)``. In production those are the guarded
model client and the tool registry; in a replay test they are served from an audit tape.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from python_wrap import ApprovalDenied, ToolNotAllowed, record_step

Ask = Callable[[list[dict[str, Any]]], Any]
Dispatch = Callable[[str, str], Any]


@dataclass
class AgentResult:
    text: str
    llm_calls: int = 0
    tool_calls: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)


def _field(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)


def run_agent(ticket: str, *, ask: Ask, dispatch: Dispatch) -> AgentResult:
    """Handle one ticket. Call this inside an ``agent_run``/``registry.run()`` block."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": ticket}]
    result = AgentResult(text="")
    while True:
        record_step("llm", "support-model")  # counts against max_steps and the budget
        reply = ask(messages)
        result.llm_calls += 1
        calls = _field(reply, "tool_calls") or []
        if not calls:
            result.text = str(_field(reply, "text") or "")
            return result
        for call in calls:
            name, arguments = _field(call, "name"), _field(call, "arguments")
            try:
                output: Any = dispatch(name, arguments)
                result.tool_calls.append(name)
            except (ApprovalDenied, ToolNotAllowed) as exc:
                # Tell the model; it can apologise or escalate instead of crashing the run.
                output = {"error": f"{type(exc).__name__}: needs a human"}
                result.denied.append(name)
            messages.append({"role": "tool", "name": name, "content": json.dumps(output)})
