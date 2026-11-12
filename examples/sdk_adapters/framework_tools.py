"""Agent-framework tools with python-wrap: replay-safe effects, one budget, loop guards, MCP.

Runs offline in well under a second with small fakes of the objects pydantic-ai and the
OpenAI Agents SDK hand to tools and hooks; no SDK, network or API key is needed. With the
real frameworks the same objects come from them: ``RunContext`` (pydantic-ai),
``ToolContext`` and ``FunctionTool`` (``agents``).

What it shows:

1. An `EffectLedger` keyed on the provider's tool-call id: a replayed call returns the
   stored result, a new call id runs, and a crash between the side effect and the record
   is detected and reconciled by a decision hook instead of charging twice.
2. pydantic-ai: `PythonWrapCapability.tool` with a timeout, a retry that leaves
   ``ModelRetry`` alone, loop detection, and run usage feeding a python-wrap `Budget`.
3. OpenAI Agents SDK: `AgentsGuard.wrap` around a sub-agent tool, so one
   ``max_tool_calls`` covers the nested run, and hooks that feed the same budget.
4. `mcp_guard` around an MCP session: per-attempt timeout, retries only for read tools,
   and a per-server circuit breaker.

Run it: ``python examples/sdk_adapters/framework_tools.py``
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import Any

from python_wrap import (
    AuditTape,
    Budget,
    CircuitOpenError,
    EffectLedger,
    EffectResolved,
    EffectUnknown,
    LoopDetected,
    MemoryEffectStore,
    StepLimitExceeded,
    mcp_guard,
    retry,
    tool,
    with_call_id,
)
from python_wrap.adapters.openai_agents import AgentsGuard
from python_wrap.adapters.pydantic_ai import PythonWrapCapability

# ------------------------------------------------------------------- 1. effect ledger


class StoreThatFailsOnce(MemoryEffectStore):
    """Simulates the process dying right after the payment went through."""

    fail_next_settle = False

    def settle(self, record: Any, ttl: float, /) -> bool:
        if self.fail_next_settle:
            self.fail_next_settle = False
            raise ConnectionError("ledger store unreachable")
        return super().settle(record, ttl)


def ledger_demo() -> None:
    payments: list[str] = []  # the "bank"
    store = StoreThatFailsOnce()

    def reconcile(unknown: EffectUnknown) -> Any:
        # Ask the payment provider whether the charge exists before doing anything.
        return EffectResolved("charged (found at provider)") if payments else "retry"

    ledger = EffectLedger(store, on_unknown=reconcile)

    @tool(ledger=ledger)
    def charge(order: str, cents: int) -> str:
        payments.append(order)
        return f"charged {order}"

    with with_call_id("call_1"):
        assert charge("A-1", 500) == "charged A-1"
        assert charge("A-1", 500) == "charged A-1"  # framework replay: stored result
    with with_call_id("call_2"):
        store.fail_next_settle = True
        with contextlib.suppress(ConnectionError):  # the charge happened, the record did not
            charge("B-2", 700)
        assert charge("B-2", 700) == "charged (found at provider)"
    assert payments == ["A-1", "B-2"]
    print("ledger: 2 payments for 4 calls, crash reconciled without a double charge")


# ------------------------------------------------------------------- 2. pydantic-ai


class ModelRetry(Exception):
    pass


@dataclass
class RunUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class RunContext:
    tool_call_id: str
    usage: RunUsage = field(default_factory=RunUsage)


@dataclass
class RunResult:
    output: str
    total: RunUsage

    def usage(self) -> RunUsage:
        return self.total


def pydantic_ai_demo(spend: Budget, tape: AuditTape) -> None:
    cap = PythonWrapCapability(
        timeout=5,
        retry=retry(max_attempts=3, delay=0, on=Exception),
        ledger=EffectLedger(),
        max_repeats=2,
        budget=spend,
        tape=tape,
    )
    attempts: list[str] = []

    @cap.tool
    def stock(ctx: RunContext, sku: str) -> int:
        """Units in stock for a SKU."""
        attempts.append(sku)
        if sku != sku.upper():
            raise ModelRetry("use the upper-case SKU")  # never retried by python-wrap
        return 7

    class FakeAgent:
        """Calls the tool like ``agent.run_sync`` would, then reports total usage."""

        def run_sync(self, prompt: str) -> RunResult:
            with contextlib.suppress(ModelRetry):
                stock(RunContext("t1", RunUsage(200, 40)), "sku-1")
            stock(RunContext("t2", RunUsage(450, 80)), "SKU-1")
            stock(RunContext("t3", RunUsage(700, 120)), "SKU-1")
            try:
                stock(RunContext("t4", RunUsage(900, 150)), "SKU-1")  # 3rd identical call
            except LoopDetected:
                return RunResult("stopped a loop", RunUsage(1000, 160))
            raise AssertionError("loop not detected")

    result = cap.run_sync(FakeAgent(), "how many SKU-1?")
    assert result.output == "stopped a loop"
    assert attempts == ["sku-1", "SKU-1", "SKU-1"]
    assert spend.spent().tokens == 1160  # the run's total, counted once
    print(f"pydantic-ai: ModelRetry passed through, loop stopped, {spend.spent().tokens} tokens")


# ------------------------------------------------------------------- 3. OpenAI Agents SDK


@dataclass
class ToolContext:
    tool_call_id: str
    tool_arguments: str = "{}"


@dataclass
class FunctionTool:
    name: str
    on_invoke_tool: Any


@dataclass
class ModelResponse:
    usage: RunUsage


@dataclass
class Agent:
    name: str
    model: str = "demo-model"


async def openai_agents_demo(spend: Budget, tape: AuditTape) -> None:
    guard = AgentsGuard(timeout=5, max_tool_calls=4, budget=spend, tape=tape)

    async def search(ctx: ToolContext, payload: str) -> str:
        return "result for " + json.loads(payload)["q"]

    search_tool = guard.wrap(FunctionTool("search", search))

    async def researcher(ctx: ToolContext, payload: str) -> str:
        """A sub-agent run as a tool: its own run, its own tool calls."""
        for index in range(10):  # a sub-agent that would happily go on
            await search_tool.on_invoke_tool(
                ToolContext(f"{ctx.tool_call_id}-{index}"), json.dumps({"q": f"page {index}"})
            )
        return "never"

    research_tool = guard.wrap(FunctionTool("researcher", researcher))
    hooks = guard.hooks()
    await hooks.on_llm_end(None, Agent("triage"), ModelResponse(RunUsage(300, 40)))
    with contextlib.suppress(StepLimitExceeded):
        await research_tool.on_invoke_tool(ToolContext("r1"), '{"q": "x"}')
    assert guard.counter.calls == 4  # researcher + 3 nested searches, then stopped
    assert spend.spent().tokens == 1160 + 340
    print("openai-agents: nested tool calls capped at 4, usage shared with pydantic-ai")


# ------------------------------------------------------------------- 4. MCP


class FakeSession:
    """The ``call_tool`` surface of an MCP ``ClientSession``."""

    name = "files"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        self.calls.append(name)
        if name == "read_file" and self.calls.count("read_file") == 1:
            raise ConnectionError("server restarting")
        if name == "write_file":
            raise ConnectionError("server down")
        return {"content": [{"type": "text", "text": "hello"}], "isError": False}


async def mcp_demo() -> None:
    session = mcp_guard(
        FakeSession(),
        timeout=5,
        retry=retry(max_attempts=3, delay=0, on=ConnectionError),
        retry_tools=["read_*"],
        breaker={"failure_threshold": 2, "recovery_timeout": 60},
    )
    reply = await session.call_tool("read_file", {"path": "a.txt"})
    assert reply["content"][0]["text"] == "hello"
    for _ in range(2):
        with contextlib.suppress(ConnectionError):
            await session.call_tool("write_file", {"path": "a.txt"})
    with contextlib.suppress(CircuitOpenError):
        await session.call_tool("read_file", {"path": "a.txt"})
    assert session.wrapped.calls == ["read_file", "read_file", "write_file", "write_file"]
    print("mcp: read retried once, writes never retried, breaker open after 2 failures")


def main() -> int:
    ledger_demo()
    spend = Budget(tokens=10_000, name="all-agents")
    tape = AuditTape()
    pydantic_ai_demo(spend, tape)
    asyncio.run(openai_agents_demo(spend, tape))
    asyncio.run(mcp_demo())
    kinds = [record.kind for record in tape.records()]
    assert kinds.count("tool") >= 4 and "llm" in kinds
    print(f"audit tape: {len(kinds)} records")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
