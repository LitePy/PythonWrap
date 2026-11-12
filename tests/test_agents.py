"""Agent tool-use safety: idempotent tools, run guards, policies, approvals, events."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from python_wrap import (
    AgentRun,
    ApprovalDenied,
    ApprovalTimeout,
    DryRun,
    LoopDetected,
    RunBudgetExceeded,
    RunCancelled,
    RunGuard,
    RunTimeout,
    StepLimitExceeded,
    TimeoutExpired,
    ToolCall,
    ToolNotAllowed,
    ToolPolicy,
    ToolRegistry,
    agent_run,
    checkpoint,
    current_run,
    record_step,
    retry,
    tool,
    tool_spec,
)
from python_wrap.agents import canonical, run_id_var
from python_wrap.doctor import doctor, explain
from python_wrap.events import listening

# ----------------------------------------------------------------- idempotent tools


def test_write_tool_replay_returns_stored_result_without_running() -> None:
    sent: list[str] = []

    @tool
    def send_email(to: str, subject: str = "hi") -> str:
        sent.append(to)
        return f"sent:{to}:{len(sent)}"

    with agent_run(run_id="r1"):
        first = send_email("a@example.com")
        again = send_email(to="a@example.com", subject="hi")  # same canonical args
    assert first == again == "sent:a@example.com:1"
    assert sent == ["a@example.com"]


def test_replay_scoped_to_run_id() -> None:
    sent: list[str] = []

    @tool
    def send(to: str) -> None:
        sent.append(to)

    with agent_run(run_id="a"):
        send("x")
    with agent_run(run_id="b"):
        send("x")
    with agent_run(run_id="a"):  # resumed run: replay
        send("x")
    assert sent == ["x", "x"]


def test_failed_call_is_not_stored() -> None:
    calls: list[int] = []

    @tool
    def charge(amount: int) -> int:
        calls.append(amount)
        if len(calls) == 1:
            raise ConnectionError
        return amount

    with pytest.raises(ConnectionError):
        charge(5)
    assert charge(5) == 5
    assert charge(5) == 5
    assert calls == [5, 5]


def test_read_only_tools_are_not_deduplicated_and_key_can_be_disabled() -> None:
    reads: list[int] = []

    @tool(read_only=True)
    def lookup(n: int) -> int:
        reads.append(n)
        return n

    @tool(idempotent_key=False)
    def write(n: int) -> int:
        reads.append(-n)
        return n

    lookup(1)
    lookup(1)
    write(2)
    write(2)
    assert reads == [1, 1, -2, -2]
    assert tool_spec(lookup).idempotent is False  # type: ignore[union-attr]
    assert tool_spec(write).idempotent is False  # type: ignore[union-attr]


def test_custom_key_is_global_business_key() -> None:
    charged: list[str] = []

    @tool(idempotent_key=lambda order_id, amount: order_id)
    def charge(order_id: str, amount: int) -> str:
        charged.append(order_id)
        return order_id

    with agent_run():
        charge("o-1", 10)
    with agent_run():
        charge("o-1", 99)
    assert charged == ["o-1"]


def test_retry_runs_inside_dedup_and_replay_skips_it() -> None:
    attempts: list[int] = []

    @tool(retry=retry(max_attempts=3, delay=0, on=ConnectionError), timeout=5)
    def post(n: int) -> int:
        attempts.append(n)
        if len(attempts) < 2:
            raise ConnectionError
        return n

    assert post(1) == 1
    assert post(1) == 1
    assert attempts == [1, 1]
    spec = tool_spec(post)
    assert spec is not None and spec.retried and spec.timeout == 5


def test_async_tool_replay() -> None:
    sent: list[str] = []

    @tool
    async def notify(user: str) -> str:
        sent.append(user)
        return user

    async def main() -> None:
        async with agent_run(run_id="x"):
            assert await notify("u") == "u"
            assert await notify("u") == "u"

    asyncio.run(main())
    assert sent == ["u"]


def test_events_call_and_replayed() -> None:
    @tool
    def act(n: int) -> int:
        return n

    seen = []
    with listening(seen.append), agent_run(run_id="ev"):
        act(1)
        act(1)
    names = [e.name for e in seen]
    assert names == [
        "agent.run.start",
        "agent.run.step",
        "tool.call",
        "agent.run.step",
        "tool.replayed",
        "agent.run.end",
    ]
    assert seen[2].function == "act" and seen[2].fields["run_id"] == "ev"
    assert seen[-1].fields["steps"] == 2 and seen[-1].fields["error"] is None


def test_canonical_is_order_independent() -> None:
    assert canonical({"a": 1, "b": {2, 1}}) == canonical({"b": {1, 2}, "a": 1})
    assert canonical({"a": 1}) != canonical({"a": 2})
    assert canonical(None) is None


def test_method_tool_ignores_self_in_arguments() -> None:
    seen: list[ToolCall] = []

    class Mailer:
        @tool(requires_approval=True, idempotent_key=False)
        def send(self, to: str) -> str:
            return to

    def approver(call: ToolCall) -> bool:
        seen.append(call)
        return True

    with agent_run(policy=ToolPolicy(approver=approver)):
        assert Mailer().send("z") == "z"
    assert dict(seen[0].arguments) == {"to": "z"}


# ----------------------------------------------------------------- run guard


def test_step_limit() -> None:
    with agent_run(max_steps=2) as run:
        record_step("llm", "m")
        record_step("llm", "m")
        with pytest.raises(StepLimitExceeded) as info:
            record_step("llm", "m")
    assert info.value.limit == 2 and info.value.run_id == run.run_id
    assert run.steps == 2
    assert [s.index for s in run.history] == [1, 2]


def test_loop_detection_counts_identical_calls_only() -> None:
    @tool(read_only=True)
    def search(q: str) -> str:
        return q

    with agent_run(max_repeats=2) as run:
        search("a")
        search("b")
        search("a")
        with pytest.raises(LoopDetected) as info:
            search("a")
    assert info.value.name == "search" and info.value.repeats == 2
    assert run.steps == 3


def test_record_step_without_arguments_never_loops() -> None:
    with agent_run(max_repeats=1):
        for _ in range(5):
            record_step("llm", "m")


def test_run_timeout_with_fake_clock(fake_clock) -> None:
    with agent_run(max_seconds="1m") as run:
        record_step("llm", "m")
        fake_clock.advance(61)
        with pytest.raises(RunTimeout) as info:
            record_step("llm", "m")
        assert isinstance(info.value, TimeoutExpired) and isinstance(info.value, TimeoutError)
        assert run.cancelled
        with pytest.raises(TimeoutExpired):
            checkpoint()


def test_real_timer_cancels_token_for_checkpoint() -> None:
    with agent_run(max_seconds=0.05) as run:
        deadline = time.monotonic() + 2
        while not run.token.cancelled and time.monotonic() < deadline:
            time.sleep(0.01)
        with pytest.raises(TimeoutExpired):
            checkpoint()


def test_cancel_and_budget() -> None:
    with agent_run() as run:
        run.cancel()
        with pytest.raises(RunCancelled):
            record_step("llm", "m")

    class Budget:
        exhausted = False

    budget = Budget()
    with agent_run(budget=budget):
        record_step("llm", "m")
        budget.exhausted = True
        with pytest.raises(RunBudgetExceeded):
            record_step("llm", "m")

    class Checking:
        def check(self) -> None:
            raise RuntimeError("spent")

    with agent_run(budget=Checking()), pytest.raises(RuntimeError, match="spent"):
        record_step("llm", "m")


def test_current_run_and_run_id_var_nest() -> None:
    assert current_run() is None and run_id_var.get() is None
    with agent_run(run_id="outer", max_steps=3) as outer:
        with agent_run(run_id="inner") as inner:
            assert current_run() is inner and inner.parent is outer
            assert run_id_var.get() == "inner"
            record_step("llm", "m")
            record_step("llm", "m")
            record_step("llm", "m")
            with pytest.raises(StepLimitExceeded) as info:
                record_step("llm", "m")  # the outer limit applies to the inner run
            assert info.value.run_id == "outer"
        assert run_id_var.get() == "outer"
        assert outer.steps == 3 and inner.steps == 3
    assert current_run() is None


def test_record_step_outside_run_is_noop() -> None:
    assert record_step("llm", "m") is None


def test_guard_is_reusable_and_thread_safe() -> None:
    guard = RunGuard(max_steps=1000)
    with guard as run:

        def work() -> None:
            for _ in range(100):
                record_step("llm", "m")

        import contextvars

        threads = [
            threading.Thread(target=contextvars.copy_context().run, args=(work,)) for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert run.steps == 500
    with guard as second:
        assert second.steps == 0 and second.run_id != run.run_id


def test_guard_as_decorator_sync_and_async() -> None:
    @agent_run(max_steps=1)
    def loop() -> str:
        record_step("llm", "m")
        run = current_run()
        assert isinstance(run, AgentRun)
        return run.run_id

    assert loop() != loop()

    @agent_run(max_steps=1)
    async def aloop() -> int:
        record_step("llm", "m")
        with pytest.raises(StepLimitExceeded):
            record_step("llm", "m")
        return 1

    assert asyncio.run(aloop()) == 1


def test_async_guard_timer() -> None:
    async def main() -> None:
        async with agent_run(max_seconds=0.02) as run:
            await asyncio.sleep(0.1)
            assert run.token.cancelled
            with pytest.raises(RunTimeout):
                record_step("llm", "m")

    asyncio.run(main())


def test_end_event_reports_error() -> None:
    seen = []
    with listening(seen.append), pytest.raises(StepLimitExceeded), agent_run(max_steps=1):
        record_step("llm", "m")
        record_step("llm", "m")
    assert seen[-1].name == "agent.run.end" and seen[-1].fields["error"] == "StepLimitExceeded"


def test_invalid_guard_settings() -> None:
    for bad in ({"max_steps": 0}, {"max_repeats": True}, {"max_seconds": -1}, {"run_id": ""}):
        with pytest.raises(ValueError):
            RunGuard(**bad)  # type: ignore[arg-type]


# ----------------------------------------------------------------- policies


def test_allow_deny_globs_and_read_only() -> None:
    @tool(read_only=True)
    def read_file(path: str) -> str:
        return path

    @tool
    def delete_file(path: str) -> None: ...

    with agent_run(policy=ToolPolicy(deny=["delete_*"])):
        assert read_file("x") == "x"
        with pytest.raises(ToolNotAllowed) as info:
            delete_file("x")
        assert info.value.reason == "denied"
    with agent_run(policy=ToolPolicy(allow=["read_*"])), pytest.raises(ToolNotAllowed):
        delete_file("x")
    with agent_run(policy=ToolPolicy(allow_write=False)):
        assert read_file("y") == "y"
        with pytest.raises(ToolNotAllowed) as info:
            delete_file("x")
        assert info.value.reason == "read_only"


def test_nested_policies_combine_strictest() -> None:
    @tool(idempotent_key=False)
    def write() -> str:
        return "wrote"

    outer = agent_run(policy=ToolPolicy(allow_write=False))
    with outer, agent_run(policy=ToolPolicy()), pytest.raises(ToolNotAllowed):
        write()


def test_dry_run_returns_declared_result_or_marker() -> None:
    ran: list[str] = []

    @tool(dry_result="would send")
    def send(to: str) -> str:
        ran.append(to)
        return "sent"

    @tool(dry_result=lambda n: n * 10)
    def scale(n: int) -> int:
        ran.append("scale")
        return n

    @tool
    def wipe(table: str) -> None:
        ran.append(table)

    @tool(read_only=True)
    def peek() -> str:
        return "real"

    seen = []
    with listening(seen.append), agent_run(policy=ToolPolicy(dry_run=True)):
        assert send("a") == "would send"
        assert scale(2) == 20
        marker = wipe("users")
        assert peek() == "real"
    assert isinstance(marker, DryRun) and not marker
    assert marker.tool == "wipe" and dict(marker.arguments) == {"table": "users"}
    assert ran == []
    assert sum(e.name == "tool.dry_run" for e in seen) == 3


def test_denied_event() -> None:
    @tool
    def nuke() -> None: ...

    seen = []
    run = agent_run(policy=ToolPolicy(deny=["*"]))
    with listening(seen.append), run, pytest.raises(ToolNotAllowed):
        nuke()
    denied = [e for e in seen if e.name == "tool.denied"]
    assert denied[0].fields["reason"] == "denied" and denied[0].function == "nuke"


# ----------------------------------------------------------------- approvals


def test_approval_required_denies_without_approver() -> None:
    ran = []

    @tool(requires_approval=True)
    def transfer(amount: int) -> None:
        ran.append(amount)

    with pytest.raises(ApprovalDenied):
        transfer(1)
    assert ran == []


@pytest.mark.parametrize(
    ("answer", "allowed"), [(True, True), (False, False), (1, False), ("yes", False)]
)
def test_sync_approver_must_return_true(answer: object, allowed: bool) -> None:
    ran = []

    @tool(requires_approval=True, idempotent_key=False)
    def transfer(amount: int) -> None:
        ran.append(amount)

    with agent_run(policy=ToolPolicy(approver=lambda call: answer)):  # type: ignore[arg-type,return-value]
        if allowed:
            transfer(5)
        else:
            with pytest.raises(ApprovalDenied):
                transfer(5)
    assert ran == ([5] if allowed else [])


def test_approver_error_and_timeout_deny() -> None:
    @tool(requires_approval=True)
    def act() -> None: ...

    def broken(call: ToolCall) -> bool:
        raise RuntimeError

    with agent_run(policy=ToolPolicy(approver=broken)), pytest.raises(ApprovalDenied) as info:
        act()
    assert isinstance(info.value.__cause__, RuntimeError)

    release = threading.Event()

    def slow(call: ToolCall) -> bool:
        release.wait(2)
        return True

    try:
        run = agent_run(policy=ToolPolicy(approver=slow, approval_timeout=0.05))
        with run, pytest.raises(ApprovalTimeout) as timed:
            act()
        assert timed.value.reason == "approval_timeout"
    finally:
        release.set()


def test_approver_sees_call_info_and_run() -> None:
    calls: list[tuple[ToolCall, str | None]] = []

    def approver(call: ToolCall) -> bool:
        calls.append((call, run_id_var.get()))
        return True

    @tool(requires_approval=True, description="Move money")
    def transfer(amount: int, to: str = "acct") -> int:
        return amount

    with agent_run(run_id="ap", policy=ToolPolicy(approver=approver)):
        transfer(7)
    call, seen_run = calls[0]
    assert call.tool == "transfer" and call.run_id == "ap" and seen_run == "ap"
    assert dict(call.arguments) == {"amount": 7, "to": "acct"}
    assert call.description == "Move money" and call.read_only is False


def test_async_approvers() -> None:
    @tool(requires_approval=True, idempotent_key=False)
    async def act(n: int) -> int:
        return n

    async def yes(call: ToolCall) -> bool:
        return True

    async def never(call: ToolCall) -> bool:
        await asyncio.sleep(5)
        return True

    async def main() -> None:
        async with agent_run(policy=ToolPolicy(approver=yes)):
            assert await act(1) == 1
        async with agent_run(policy=ToolPolicy(approver=lambda call: True)):
            assert await act(2) == 2
        async with agent_run(policy=ToolPolicy(approver=never, approval_timeout=0.02)):
            with pytest.raises(ApprovalTimeout):
                await act(3)

    asyncio.run(main())


def test_async_approver_used_from_sync_tool() -> None:
    async def yes(call: ToolCall) -> bool:
        return True

    @tool(requires_approval=True)
    def act() -> str:
        return "ok"

    with agent_run(policy=ToolPolicy(approver=yes)):
        assert act() == "ok"


# ----------------------------------------------------------------- registry


def test_registry_dispatch_and_policy() -> None:
    tools = ToolRegistry(policy=ToolPolicy(deny=["drop_*"]), guard=RunGuard(max_steps=5))

    @tools.tool(read_only=True)
    def lookup(sku: str) -> int:
        return len(sku)

    @tools.tool
    def drop_table(name: str) -> None: ...

    assert "lookup" in tools and len(tools) == 2 and list(tools) == ["lookup", "drop_table"]
    assert tools.call("lookup", '{"sku": "abc"}') == 3
    with pytest.raises(ToolNotAllowed):
        tools.call("drop_table", {"name": "t"})  # registry policy outside a run too
    with pytest.raises(ToolNotAllowed) as info:
        tools.call("missing")
    assert info.value.reason == "unknown"
    with pytest.raises(ValueError):
        tools.call("lookup", "[1]")
    with tools.run(run_id="reg") as run:
        drop = tools.get("drop_table")
        with pytest.raises(ToolNotAllowed):
            drop(name="t")  # direct call inside the registry's run
        assert run.run_id == "reg"
    with pytest.raises(ValueError):
        tools.add(lookup)


def test_registry_async_tool_and_plain_function() -> None:
    def plain(x: int) -> int:
        return x

    tools = ToolRegistry([plain])

    @tools.tool(read_only=True)
    async def fetch(x: int) -> int:
        return x + 1

    assert tools.call("plain", {"x": 1}) == 1
    assert asyncio.run(tools.call("fetch", {"x": 1})) == 2
    assert [s.name for s in tools.specs()] == ["plain", "fetch"]


# ----------------------------------------------------------------- doctor


def codes(report) -> list[str]:  # type: ignore[no-untyped-def]
    return [f.code for f in report.findings]


def test_pw016_retried_write_tool_without_key() -> None:
    @tool(idempotent_key=False, retry=retry(max_attempts=3, on=ConnectionError))
    def send() -> None: ...

    @tool(retry=retry(max_attempts=3, on=ConnectionError))
    def send_safe() -> None: ...

    @retry(max_attempts=3, on=ConnectionError)
    @tool(idempotent_key=False)
    def outer() -> None: ...

    @tool(read_only=True, retry=retry(max_attempts=3, on=ConnectionError))
    def read() -> None: ...

    assert "PW016" in codes(doctor(send))
    assert not doctor(send).ok
    assert "PW016" in codes(doctor(outer))
    assert "PW016" not in codes(doctor(send_safe))
    assert "PW016" not in codes(doctor(read))
    assert [layer.role for layer in doctor(send).layers][:2] == ["tool", "retry"]


def test_pw017_unbounded_runs() -> None:
    assert "PW017" in codes(explain(RunGuard()))
    assert "PW017" not in codes(explain(RunGuard(max_steps=10)))

    @agent_run()
    def loop() -> None: ...

    @agent_run(max_seconds=30)
    def bounded() -> None: ...

    assert "PW017" in codes(doctor(loop))
    assert "PW017" not in codes(doctor(bounded))

    tools = ToolRegistry([tool(idempotent_key=False, retry=retry(max_attempts=2))(lambda: None)])
    report = explain(tools)
    assert "PW017" in codes(report) and "PW016" in codes(report)
    assert "PW017" not in codes(explain(ToolRegistry(guard=RunGuard(max_steps=3))))


def test_llm_budget_opens_run_scope_and_surfaces_as_run_error() -> None:
    from python_wrap.llm import Budget as LlmBudget
    from python_wrap.llm import BudgetExceeded

    spend = LlmBudget(tokens=100, per="run")

    @spend
    def call() -> dict[str, object]:
        return {"usage": {"input_tokens": 60, "output_tokens": 50}}

    with pytest.raises(RuntimeError):
        call()  # no run active
    with agent_run(budget=spend):
        record_step("llm", "m")
        call()  # crosses the limit but is returned
        with pytest.raises(RunBudgetExceeded):
            record_step("llm", "m")
        with pytest.raises(BudgetExceeded):
            call()  # direct calls keep their own error
    with agent_run(budget=spend):  # a fresh run starts from zero
        record_step("llm", "m")
        call()


def test_llm_total_budget_checked_by_run() -> None:
    from python_wrap.llm import Budget as LlmBudget

    spend = LlmBudget(tokens=10)

    @spend
    def call() -> dict[str, object]:
        return {"usage": {"prompt_tokens": 8, "completion_tokens": 8}}

    with agent_run(budget=spend):
        call()
        with pytest.raises(RunBudgetExceeded):
            record_step("llm", "m")


def test_llm_budget_run_scope_async() -> None:
    from python_wrap.llm import Budget as LlmBudget

    spend = LlmBudget(tokens=5, per="chain")

    @spend
    async def call() -> dict[str, object]:
        return {"usage": {"input_tokens": 4, "output_tokens": 4}}

    async def main() -> None:
        async with agent_run(budget=spend):
            await call()
            with pytest.raises(RunBudgetExceeded):
                record_step("llm", "m")

    asyncio.run(main())
