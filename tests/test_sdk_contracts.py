"""Contract tests against the real pydantic-ai and OpenAI Agents SDK, when installed.

They run offline: pydantic-ai's ``FunctionModel`` and a scripted ``agents`` model stand in
for a provider. Each group skips cleanly when its framework is not importable; install
``python-wrap[pydantic-ai]`` / ``python-wrap[openai-agents]`` to run them.
"""

from __future__ import annotations

import importlib.util
import json
from typing import Any

import pytest

from python_wrap import (
    AuditTape,
    Budget,
    EffectLedger,
    LoopDetected,
    StepLimitExceeded,
    retry,
)

pytestmark = pytest.mark.sdk

HAS_PYDANTIC_AI = importlib.util.find_spec("pydantic_ai") is not None
HAS_AGENTS = importlib.util.find_spec("agents") is not None

needs_pydantic_ai = pytest.mark.skipif(not HAS_PYDANTIC_AI, reason="pydantic-ai not installed")
needs_agents = pytest.mark.skipif(not HAS_AGENTS, reason="openai-agents not installed")


# ---------------------------------------------------------------- pydantic-ai


def _scripted(calls: list[tuple[str, dict[str, Any]]], final: str = "done") -> Any:
    """A FunctionModel that asks for ``calls`` one per turn, then answers ``final``."""
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    queue = list(calls)

    def respond(messages: list[Any], info: Any) -> Any:
        if queue:
            name, args = queue.pop(0)
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart(final)])

    return FunctionModel(respond)


@needs_pydantic_ai
def test_pydantic_ai_toolset_ledger_audit_and_usage() -> None:
    from pydantic_ai import Agent

    from python_wrap.adapters.pydantic_ai import PythonWrapCapability

    tape = AuditTape()
    spend = Budget(tokens=1_000_000)
    cap = PythonWrapCapability(ledger=EffectLedger(), tape=tape, budget=spend, timeout=10)
    notes: list[str] = []

    def add_note(ticket: str, text: str) -> str:
        """Attach a note to a ticket."""
        notes.append(ticket)
        return f"noted {ticket}"

    model = _scripted([("add_note", {"ticket": "T-1", "text": "hi"})] * 2)
    agent = Agent(model, toolsets=[cap.toolset(add_note)])
    result = cap.run_sync(agent, "note it twice")
    assert result.output == "done"
    assert notes == ["T-1", "T-1"]  # two model calls, two tool_call_ids: both run
    tools = [r for r in tape.records() if r.kind == "tool"]
    assert [r.name for r in tools] == ["add_note", "add_note"]
    call_ids = {r.input["call_id"] for r in tools}
    assert len(call_ids) == 2 and None not in call_ids
    usage = result.usage()
    assert spend.spent().tokens == usage.input_tokens + usage.output_tokens > 0


@needs_pydantic_ai
def test_pydantic_ai_model_retry_is_not_retried_by_python_wrap() -> None:
    from pydantic_ai import Agent, ModelRetry, RunContext

    from python_wrap.adapters.pydantic_ai import PythonWrapCapability

    cap = PythonWrapCapability(retry=retry(max_attempts=5, delay=0, on=Exception))
    calls: list[str] = []

    @cap.tool
    def stock(ctx: RunContext[None], sku: str) -> int:
        """Units in stock."""
        calls.append(sku)
        if sku != sku.upper():
            raise ModelRetry("use upper case")
        return 4

    model = _scripted([("stock", {"sku": "abc"}), ("stock", {"sku": "ABC"})])
    agent = Agent(model)
    agent.tool(stock)
    assert cap.run_sync(agent, "how many").output == "done"
    assert calls == ["abc", "ABC"]  # ModelRetry went back to the model once, not 5 times


@needs_pydantic_ai
def test_pydantic_ai_loop_detection_stops_the_run() -> None:
    from pydantic_ai import Agent

    from python_wrap.adapters.pydantic_ai import PythonWrapCapability

    cap = PythonWrapCapability(max_repeats=2)

    def search(query: str) -> str:
        """Search."""
        return "nothing"

    agent = Agent(_scripted([("search", {"query": "x"})] * 5), toolsets=[cap.toolset(search)])
    with pytest.raises(LoopDetected):
        cap.run_sync(agent, "loop")


@needs_pydantic_ai
async def test_pydantic_ai_async_tool_plain_and_tool_call_id_replay() -> None:
    from pydantic_ai import Agent

    from python_wrap.adapters.pydantic_ai import PythonWrapCapability

    ledger = EffectLedger()
    cap = PythonWrapCapability(ledger=ledger, max_tool_calls=5)
    sent: list[str] = []

    async def send(to: str) -> str:
        """Send."""
        sent.append(to)
        return "ok"

    agent = Agent(_scripted([("send", {"to": "a"})]), toolsets=[cap.toolset(send)])
    result = await cap.run(agent, "send")
    assert result.output == "done" and sent == ["a"]
    records = [ledger.lookup(key) for key in list(ledger.store._data)]  # type: ignore[attr-defined]
    assert records and records[0] is not None and records[0].state == "succeeded"
    assert records[0].call_id


# ---------------------------------------------------------------- openai agents


def _agents_model(turns: list[list[Any]]) -> Any:
    from agents import ModelResponse, Usage
    from agents.models.interface import Model

    class ScriptedModel(Model):
        def __init__(self) -> None:
            self.turns = list(turns)

        async def get_response(self, *args: Any, **kwargs: Any) -> Any:
            output = self.turns.pop(0) if self.turns else [_message("done")]
            return ModelResponse(
                output=output,
                usage=Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15),
                response_id=None,
            )

        def stream_response(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

    return ScriptedModel()


def _message(text: str) -> Any:
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText

    return ResponseOutputMessage(
        id="msg_1",
        type="message",
        role="assistant",
        status="completed",
        content=[ResponseOutputText(type="output_text", text=text, annotations=[])],
    )


def _call(name: str, args: dict[str, Any], call_id: str) -> Any:
    from openai.types.responses import ResponseFunctionToolCall

    return ResponseFunctionToolCall(
        id=f"fc_{call_id}",
        type="function_call",
        call_id=call_id,
        name=name,
        arguments=json.dumps(args),
        status="completed",
    )


@needs_agents
async def test_openai_agents_function_tool_hooks_budget_and_tape() -> None:
    from agents import Agent, Runner

    from python_wrap.adapters.openai_agents import AgentsGuard

    tape = AuditTape()
    spend = Budget(tokens=10_000)
    guard = AgentsGuard(ledger=EffectLedger(), tape=tape, budget=spend, timeout=10)
    created: list[str] = []

    @guard.function_tool
    def create_ticket(title: str) -> str:
        """Open a ticket."""
        created.append(title)
        return "T-1"

    model = _agents_model(
        [
            [_call("create_ticket", {"title": "login"}, "call_1")],
            [_message("opened T-1")],
        ]
    )
    agent = Agent(name="triage", model=model, tools=[create_ticket])
    result = await Runner.run(agent, "help", hooks=guard.hooks())
    assert result.final_output == "opened T-1"
    assert created == ["login"]
    assert spend.spent().tokens == 30  # two model responses of 15 tokens
    kinds = [record.kind for record in tape.records()]
    assert kinds.count("tool") == 1 and kinds.count("llm") == 2
    tool_record = next(r for r in tape.records() if r.kind == "tool")
    assert tool_record.input["call_id"] == "call_1"


@needs_agents
async def test_openai_agents_loop_detection_ends_the_run() -> None:
    from agents import Agent, Runner

    from python_wrap.adapters.openai_agents import AgentsGuard

    guard = AgentsGuard(max_repeats=2)

    @guard.function_tool
    def search(query: str) -> str:
        """Search."""
        return "nothing"

    turns = [[_call("search", {"query": "x"}, f"call_{i}")] for i in range(5)]
    agent = Agent(name="looper", model=_agents_model(turns), tools=[search])
    with pytest.raises(LoopDetected):
        await Runner.run(agent, "go", hooks=guard.hooks())


@needs_agents
async def test_openai_agents_sub_agent_tool_shares_max_tool_calls() -> None:
    from agents import Agent, Runner

    from python_wrap.adapters.openai_agents import AgentsGuard

    guard = AgentsGuard(max_tool_calls=3)

    @guard.function_tool
    def lookup(page: int) -> str:
        """Look up a page."""
        return f"page {page}"

    sub_turns = [[_call("lookup", {"page": i}, f"sub_{i}")] for i in range(10)]
    researcher = Agent(name="researcher", model=_agents_model(sub_turns), tools=[lookup])
    research = guard.wrap(researcher.as_tool("research", "Research a topic", max_turns=20))
    top = Agent(
        name="lead",
        model=_agents_model([[_call("research", {"input": "topic"}, "top_1")]]),
        tools=[research],
    )
    with pytest.raises(StepLimitExceeded):
        await Runner.run(top, "go", hooks=guard.hooks())
    assert guard.counter.calls == 3  # research + 2 nested lookups, across both runs


@needs_agents
def test_openai_agents_hooks_are_real_hook_subclasses() -> None:
    from agents import AgentHooks, RunHooks

    from python_wrap.adapters.openai_agents import AgentsGuard

    guard = AgentsGuard()
    assert isinstance(guard.hooks(), RunHooks.__origin__)  # type: ignore[attr-defined]
    assert isinstance(guard.agent_hooks(), AgentHooks.__origin__)  # type: ignore[attr-defined]
