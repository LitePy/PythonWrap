"""pydantic-ai and OpenAI Agents SDK adapters, against faithful fakes of their object shapes.

The fakes mirror what the real frameworks hand to tools and hooks:

* pydantic-ai: ``RunContext`` with ``tool_call_id``, ``tool_name``, ``retry`` and a running
  ``usage`` (``input_tokens`` / ``output_tokens``); ``ModelRetry`` asks the model to retry;
  a run result whose ``usage()`` is the final total.
* OpenAI Agents SDK: ``FunctionTool(name, on_invoke_tool)`` where ``on_invoke_tool(ctx,
  input_json)`` catches tool errors and returns a message for the model; ``ToolContext``
  with ``tool_call_id`` / ``tool_arguments``; hooks get ``ModelResponse.usage``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from dataclasses import dataclass, field
from typing import Any

import pytest

from python_wrap import (
    AuditTape,
    Budget,
    BudgetExceeded,
    EffectLedger,
    EffectUnknown,
    LoopDetected,
    RunGuard,
    StepLimitExceeded,
    TimeoutExpired,
    ToolNotAllowed,
    ToolPolicy,
    agent_run,
    retry,
)
from python_wrap.adapters._frameworks import CallCounter, UsageBridge, usage_totals
from python_wrap.adapters.openai_agents import AgentsGuard
from python_wrap.adapters.pydantic_ai import PythonWrapCapability, guard_tool
from python_wrap.doctor import doctor

# ---------------------------------------------------------------- pydantic-ai fakes


class ModelRetry(Exception):
    """Look-alike of ``pydantic_ai.ModelRetry``."""


@dataclass
class RunUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0


@dataclass
class RunContext:
    tool_call_id: str | None = None
    tool_name: str | None = None
    retry: int = 0
    usage: RunUsage = field(default_factory=RunUsage)


@dataclass
class RunResult:
    output: str
    final: RunUsage

    def usage(self) -> RunUsage:
        return self.final


# ---------------------------------------------------------------- pydantic-ai


async def test_toolset_call_keys_ledger_on_tool_call_id_and_audits() -> None:
    tape = AuditTape()
    cap = PythonWrapCapability(ledger=EffectLedger(), tape=tape, timeout=5)
    effects: list[dict[str, Any]] = []

    async def downstream(args: dict[str, Any]) -> str:
        effects.append(args)
        return f"refunded {args['order']}"

    for call_id in ("tc_1", "tc_1", "tc_2"):
        args = {"order": "A-1"}
        result = await cap.call(
            "refund", args, RunContext(call_id, "refund"), lambda a=args: downstream(a)
        )
        assert result == "refunded A-1"
    assert len(effects) == 2  # tc_1 replayed once
    records = tape.records()
    assert [r.name for r in records] == ["refund"] * 3
    assert records[0].input["call_id"] == "tc_1"
    assert records[0].output == "refunded A-1"


async def test_model_retry_passes_through_retry_untouched() -> None:
    attempts: list[int] = []
    cap = PythonWrapCapability(retry=retry(max_attempts=5, delay=0, on=Exception))

    @cap.tool
    def lookup(ctx: RunContext, sku: str) -> int:
        """Stock level of a SKU."""
        attempts.append(1)
        if len(attempts) == 1:
            raise ModelRetry("sku must be upper case")
        if len(attempts) == 2:
            raise ConnectionError("flaky")
        return 3

    with pytest.raises(ModelRetry):
        lookup(RunContext("c1"), "abc")
    assert attempts == [1]  # not retried
    assert lookup(RunContext("c2"), "ABC") == 3  # ConnectionError retried
    assert attempts == [1, 1, 1]
    assert lookup.__doc__ == "Stock level of a SKU."
    assert lookup.__name__ == "lookup"


async def test_async_tool_timeout_and_pw020() -> None:
    cap = PythonWrapCapability(timeout=0.05)

    @cap.tool
    async def slow(ctx: RunContext) -> str:
        await asyncio.sleep(1)
        return "late"

    with pytest.raises(TimeoutExpired):
        await slow(RunContext("c1"))
    assert "PW020" not in [f.code for f in doctor(slow).findings]

    unbounded = PythonWrapCapability().tool(lambda: 1, name="free")
    assert "PW020" in [f.code for f in doctor(unbounded).findings]


async def test_loop_detection_and_max_tool_calls_with_ctx_left_out_of_digest() -> None:
    cap = PythonWrapCapability(max_repeats=2, max_tool_calls=4)

    @cap.tool
    def search(ctx: RunContext, query: str) -> str:
        return query

    search(RunContext("a"), "x")
    search(RunContext("b"), "x")  # a different ctx, same arguments: identical call
    with pytest.raises(LoopDetected):
        search(RunContext("c"), "x")
    search(RunContext("d"), "y")
    search(RunContext("e"), "z")
    with pytest.raises(StepLimitExceeded):
        search(RunContext("f"), "w")
    cap.reset()
    assert search(RunContext("g"), "x") == "x"


def test_run_guard_sees_tool_steps_and_repeats() -> None:
    cap = PythonWrapCapability(guard=RunGuard(max_repeats=1))

    @cap.tool
    def ping(ctx: RunContext, host: str) -> str:
        return host

    class Agent:
        def run_sync(self, prompt: str) -> RunResult:
            ping(RunContext("1"), "h")
            ping(RunContext("2"), "h")
            return RunResult("never", RunUsage())

    with pytest.raises(LoopDetected):
        cap.run_sync(Agent(), "go")


def test_usage_bridge_feeds_budget_across_tool_calls_and_final_result() -> None:
    spend = Budget(tokens=1000)
    tape = AuditTape()
    cap = PythonWrapCapability(budget=spend, tape=tape)

    @cap.tool
    def step(ctx: RunContext) -> str:
        return "ok"

    class Agent:
        def run_sync(self, prompt: str) -> RunResult:
            step(RunContext("1", usage=RunUsage(100, 20)))
            step(RunContext("2", usage=RunUsage(300, 50)))
            return RunResult("done", RunUsage(400, 100))

    cap.run_sync(Agent(), "go")
    assert spend.spent().tokens == 500  # counted once, not 120 + 350 + 500
    llm = [r for r in tape.records() if r.kind == "llm"]
    assert llm[0].input_tokens == 400 and llm[0].output_tokens == 100

    cap.run_sync(Agent(), "again")  # a new run: totals start over
    assert spend.spent().tokens == 1000
    with pytest.raises(BudgetExceeded):
        cap.run_sync(Agent(), "third")


def test_budget_refuses_tool_call_mid_run() -> None:
    spend = Budget(tokens=100)
    cap = PythonWrapCapability(budget=spend)

    @cap.tool
    def step(ctx: RunContext) -> str:
        return "ok"

    step(RunContext("1", usage=RunUsage(50, 20)))
    with pytest.raises(BudgetExceeded):
        step(RunContext("2", usage=RunUsage(90, 30)))


async def test_async_run_with_per_run_budget_scope() -> None:
    spend = Budget(tokens=50, per="run")
    cap = PythonWrapCapability(budget=spend)

    class Agent:
        async def run(self, prompt: str) -> RunResult:
            return RunResult("ok", RunUsage(30, 30))

    await cap.run(Agent(), "a")
    await cap.run(Agent(), "b")  # each run gets a fresh scope


def test_policy_denial_raises_or_becomes_model_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = PythonWrapCapability()

    @cap.tool
    def drop(ctx: RunContext, table: str) -> str:
        return "dropped"

    with agent_run(policy=ToolPolicy(deny=["drop"])), pytest.raises(ToolNotAllowed):
        drop(RunContext("1"), "t")

    import python_wrap.adapters.pydantic_ai as adapter

    monkeypatch.setattr(adapter, "_model_retry", lambda message: ModelRetry(message))
    soft = PythonWrapCapability(on_denied="retry")
    soft_drop = soft.tool(lambda ctx, table: "dropped", name="drop")
    with agent_run(policy=ToolPolicy(deny=["drop"])), pytest.raises(ModelRetry):
        soft_drop(RunContext("2"), "t")


def test_approval_globs_use_the_run_approver() -> None:
    asked: list[str] = []
    cap = PythonWrapCapability(approval=["send_*"], read_only=["get_*"])

    @cap.tool
    def send_mail(ctx: RunContext, to: str) -> str:
        return to

    @cap.tool
    def get_mail(ctx: RunContext) -> str:
        return "inbox"

    approve = ToolPolicy(approver=lambda call: asked.append(call.tool) or True, allow_write=True)
    with agent_run(policy=approve):
        assert send_mail(RunContext("1"), "a@example.com") == "a@example.com"
    assert asked == ["send_mail"]
    with agent_run(policy=ToolPolicy(allow_write=False)):
        assert get_mail(RunContext("2")) == "inbox"


def test_guard_tool_shorthand_and_validation() -> None:
    @guard_tool(timeout=1)
    def hello(name: str) -> str:
        return name

    assert hello("x") == "x"
    assert guard_tool(lambda: 2, name="two")() == 2
    with pytest.raises(ValueError):
        PythonWrapCapability(on_denied="ignore")
    with pytest.raises(TypeError):
        PythonWrapCapability(ledger="x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PythonWrapCapability(policy="x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PythonWrapCapability(guard="x")  # type: ignore[arg-type]


def test_wrap_needs_pydantic_ai() -> None:
    try:
        import pydantic_ai  # noqa: F401
    except ImportError:
        with pytest.raises(ImportError):
            PythonWrapCapability().wrap(object())
    else:  # pragma: no cover - exercised by the sdk contract tests
        pytest.skip("pydantic-ai installed")


def test_concurrent_tool_calls_same_call_id_run_once_threads() -> None:
    cap = PythonWrapCapability(ledger=EffectLedger(poll=0.005))
    runs: list[int] = []
    barrier = threading.Barrier(8)

    @cap.tool
    def charge(ctx: RunContext, cents: int) -> str:
        runs.append(cents)
        return "charged"

    results: list[str] = []

    def worker() -> None:
        barrier.wait()
        results.append(charge(RunContext("same"), 5))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert runs == [5] and results == ["charged"] * 8


# ---------------------------------------------------------------- openai agents fakes


@dataclass
class ToolContext:
    tool_call_id: str
    tool_name: str = ""
    tool_arguments: str = "{}"
    context: Any = None


@dataclass
class FunctionTool:
    """Same fields the SDK's ``FunctionTool`` has that the guard touches."""

    name: str
    on_invoke_tool: Any
    description: str = ""


def sdk_function_tool(func: Any) -> FunctionTool:
    """What ``agents.function_tool`` does: parse JSON, call, turn errors into text."""

    async def on_invoke(ctx: ToolContext, payload: str) -> Any:
        try:
            kwargs = json.loads(payload or "{}")
            if inspect.iscoroutinefunction(func):
                return await func(**kwargs)
            return await asyncio.to_thread(func, **kwargs)
        except Exception as exc:
            return f"An error occurred while running the tool: {exc}"

    return FunctionTool(func.__name__, on_invoke, func.__doc__ or "")


@dataclass
class ModelResponse:
    usage: Any


@dataclass
class Agent:
    name: str
    model: str | None = None


# ---------------------------------------------------------------- openai agents


async def test_function_tool_wrapper_ledger_and_fatal_reraise() -> None:
    tape = AuditTape()
    guard = AgentsGuard(ledger=EffectLedger(), tape=tape, max_repeats=5, timeout=5)
    sent: list[str] = []

    def send(to: str) -> str:
        """Send a message."""
        sent.append(to)
        return f"sent {to}"

    ft = guard._wrap(sdk_function_tool(guard.guard_function(send)), inner_guarded=True)
    payload = json.dumps({"to": "ops"})
    assert await ft.on_invoke_tool(ToolContext("call_1"), payload) == "sent ops"
    assert await ft.on_invoke_tool(ToolContext("call_1"), payload) == "sent ops"
    assert await ft.on_invoke_tool(ToolContext("call_2"), payload) == "sent ops"
    assert sent == ["ops", "ops"]
    assert [r.input["call_id"] for r in tape.records()] == ["call_1", "call_1", "call_2"]


async def test_fatal_errors_end_the_run_even_inside_sdk_error_handling() -> None:
    store_ledger = EffectLedger(on_unknown=None)
    guard = AgentsGuard(ledger=store_ledger)

    def transfer(cents: int) -> str:
        raise TimeoutError("bank did not answer")  # uncertain: may have happened

    ft = guard._wrap(sdk_function_tool(guard.guard_function(transfer)), inner_guarded=True)
    message = await ft.on_invoke_tool(ToolContext("c1"), '{"cents": 5}')
    assert "error occurred" in message  # an ordinary failure is shown to the model
    with pytest.raises(EffectUnknown):
        await ft.on_invoke_tool(ToolContext("c1"), '{"cents": 5}')


async def test_denied_tool_is_reported_to_model_not_fatal() -> None:
    guard = AgentsGuard()

    def wipe() -> str:
        return "wiped"

    ft = guard._wrap(sdk_function_tool(guard.guard_function(wipe)), inner_guarded=True)
    async with agent_run(policy=ToolPolicy(deny=["wipe"])):
        message = await ft.on_invoke_tool(ToolContext("c"), "{}")
    assert "not allowed" in message


async def test_wrap_sub_agent_tool_counts_across_depth_and_applies_ledger() -> None:
    guard = AgentsGuard(max_tool_calls=3, ledger=EffectLedger(), timeout=5)
    inner_runs: list[str] = []

    def lookup(q: str) -> str:
        inner_runs.append(q)
        return q.upper()

    inner = guard._wrap(sdk_function_tool(guard.guard_function(lookup)), inner_guarded=True)

    async def sub_agent(ctx: ToolContext, payload: str) -> str:
        """What Agent.as_tool() returns: a nested run that calls its own tools."""
        args = json.loads(payload)
        first = await inner.on_invoke_tool(ToolContext(ctx.tool_call_id + "-a"), payload)
        second = await inner.on_invoke_tool(
            ToolContext(ctx.tool_call_id + "-b"), json.dumps({"q": args["q"] + "!"})
        )
        return f"{first} {second}"

    researcher = guard.wrap(FunctionTool("researcher", sub_agent))
    assert await researcher.on_invoke_tool(ToolContext("r1"), '{"q": "x"}') == "X X!"
    # replay of the same sub-agent call: stored, nothing counted below it again ...
    with pytest.raises(StepLimitExceeded):  # ... but the wrapper itself counts (4th call)
        await researcher.on_invoke_tool(ToolContext("r1"), '{"q": "x"}')
    assert inner_runs == ["x", "x!"]
    assert doctor(researcher).layers[0].role == "framework_tool"


async def test_wrap_applies_timeout_to_sub_agent_and_validates() -> None:
    guard = AgentsGuard(timeout=0.05)

    async def stuck(ctx: ToolContext, payload: str) -> str:
        await asyncio.sleep(1)
        return "late"

    wrapped = guard.wrap(FunctionTool("stuck", stuck))
    with pytest.raises(TimeoutExpired):
        await wrapped.on_invoke_tool(ToolContext("c"), "{}")
    with pytest.raises(TypeError):
        guard.wrap(object())
    with pytest.raises(TypeError):
        AgentsGuard(ledger="x")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        AgentsGuard(policy="x")  # type: ignore[arg-type]
    assert "PW020" in [
        f.code for f in doctor(AgentsGuard().wrap(FunctionTool("t", stuck))).findings
    ]


async def test_hooks_feed_budget_tape_and_limits() -> None:
    spend = Budget(tokens=100)
    tape = AuditTape()
    guard = AgentsGuard(budget=spend, tape=tape, max_tool_calls=2, max_repeats=1)
    hooks = guard.hooks()
    agent = Agent("triage", model="m-1")
    ctx = ToolContext("c1", tool_arguments='{"a": 1}')
    plain = FunctionTool("plain", None)

    await hooks.on_agent_start(ctx, agent)
    await hooks.on_llm_start(ctx, agent, "system", [])
    await hooks.on_llm_end(ctx, agent, ModelResponse(RunUsage(40, 20)))
    assert spend.spent().tokens == 60
    await hooks.on_tool_start(ctx, agent, plain)
    await hooks.on_tool_end(ctx, agent, plain, "result")
    with pytest.raises(LoopDetected):
        await hooks.on_tool_start(ctx, agent, plain)
    await hooks.on_tool_start(ToolContext("c2", tool_arguments='{"a": 2}'), agent, plain)
    with pytest.raises(StepLimitExceeded):
        await hooks.on_tool_start(ToolContext("c3", tool_arguments='{"a": 3}'), agent, plain)
    await hooks.on_llm_end(ctx, agent, ModelResponse(RunUsage(30, 20)))
    with pytest.raises(BudgetExceeded):
        await hooks.on_llm_start(ctx, agent, "system", [])
    await hooks.on_handoff(ctx, agent, agent)
    await hooks.on_agent_end(ctx, agent, "out")
    kinds = [record.kind for record in tape.records()]
    assert kinds == ["llm", "tool", "llm"]

    sub = guard.agent_hooks()
    await sub.on_start(ctx, agent)
    await sub.on_end(ctx, agent, "x")
    await sub.on_handoff(ctx, agent, agent)


async def test_hooks_skip_tools_already_guarded() -> None:
    guard = AgentsGuard(max_tool_calls=1)
    hooks = guard.hooks()

    def noop() -> str:
        return "ok"

    ft = guard._wrap(sdk_function_tool(guard.guard_function(noop)), inner_guarded=True)
    ctx = ToolContext("c1")
    await hooks.on_tool_start(ctx, Agent("a"), ft)
    assert await ft.on_invoke_tool(ctx, "{}") == "ok"  # counted once, by the wrapper
    await hooks.on_tool_end(ctx, Agent("a"), ft, "ok")


async def test_hooks_record_steps_in_run_guard() -> None:
    guard = AgentsGuard()
    hooks = guard.hooks()
    async with agent_run(max_steps=1) as run:
        await hooks.on_tool_start(
            ToolContext("c", tool_arguments="{}"), Agent("a"), FunctionTool("t", None)
        )
        assert run.steps == 1
        with pytest.raises(StepLimitExceeded):
            await hooks.on_tool_start(ToolContext("d"), Agent("a"), FunctionTool("u", None))


async def test_concurrent_parallel_tool_calls_asyncio() -> None:
    guard = AgentsGuard(ledger=EffectLedger(poll=0.005))
    runs: list[int] = []

    async def act(n: int) -> int:
        runs.append(n)
        await asyncio.sleep(0.01)
        return n

    ft = guard._wrap(sdk_function_tool(guard.guard_function(act)), inner_guarded=True)
    results = await asyncio.gather(
        *(ft.on_invoke_tool(ToolContext("same"), '{"n": 1}') for _ in range(6)),
        ft.on_invoke_tool(ToolContext("other"), '{"n": 2}'),
    )
    assert sorted(runs) == [1, 2] and results == [1] * 6 + [2]


def test_sdk_entry_points_need_the_sdk() -> None:
    try:
        import agents  # noqa: F401
    except ImportError:
        guard = AgentsGuard()
        with pytest.raises(ImportError):
            guard.function_tool(lambda: 1)
        with pytest.raises(ImportError):
            guard.run_sync(object(), "x")
    else:  # pragma: no cover - exercised by the sdk contract tests
        pytest.skip("openai-agents installed")


# ---------------------------------------------------------------- shared helpers


def test_usage_totals_reads_every_shape() -> None:
    assert usage_totals(RunUsage(3, 4)) == (3, 4)
    assert usage_totals({"usage": {"prompt_tokens": 1, "completion_tokens": 2}}) == (1, 2)
    assert usage_totals(RunResult("x", RunUsage(5, 6))) == (5, 6)

    @dataclass
    class Old:
        request_tokens: int = 7
        response_tokens: int | None = None

    assert usage_totals(Old()) == (7, 0)
    assert usage_totals(object()) is None


def test_usage_bridge_without_budget_and_counter_validation() -> None:
    bridge = UsageBridge(None)
    assert bridge.sync(RunUsage(1, 1)) is None
    bridge.check("x")
    with pytest.raises(ValueError):
        CallCounter(0, None)
    keyed = UsageBridge(Budget(tokens=10, per="key", key=lambda: "k"))
    keyed.check("x")  # a key budget outside a call: nothing to check


def test_tape_failures_never_break_tool_calls() -> None:
    class BrokenTape:
        redactor = None

        def record(self, **fields: Any) -> None:
            raise OSError("disk full")

    cap = PythonWrapCapability(tape=BrokenTape())
    assert cap.tool(lambda: "ok", name="t")() == "ok"
    guard = AgentsGuard(tape=BrokenTape())

    async def go() -> None:
        await guard.hooks().on_llm_end(None, Agent("a"), ModelResponse(RunUsage(1, 1)))

    asyncio.run(go())


def test_framework_tools_example_runs() -> None:
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    script = root / "examples" / "sdk_adapters" / "framework_tools.py"
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    result = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=30, env=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "audit tape" in result.stdout
