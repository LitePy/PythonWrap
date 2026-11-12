"""Story: a support agent uses tools safely and leaves a replayable, redacted audit tape."""

from __future__ import annotations

import json
from typing import Any

import pytest

from python_wrap import (
    ApprovalDenied,
    AuditTape,
    DryRun,
    RunBudgetExceeded,
    RunGuard,
    ToolCall,
    ToolPolicy,
    ToolRegistry,
    agent_run,
    fake_llm,
    record_step,
    replay,
    traced_llm,
    traced_tool,
)
from python_wrap.llm import Budget

pytestmark = pytest.mark.e2e


class Desk:
    def __init__(self) -> None:
        self.tape = AuditTape()
        self.sent: list[str] = []
        self.refunds: list[int] = []
        self.tools = ToolRegistry(guard=RunGuard(max_steps=20, max_repeats=3))
        self.model = fake_llm(["ok"], default="ok", usage=(30, 10))

        @self.tools.tool(read_only=True)
        def lookup_order(order_id: str) -> dict[str, object]:
            """Fetch an order."""
            return {"id": order_id, "status": "late"}

        @self.tools.tool
        @traced_tool("send_reply", tape=self.tape)
        def send_reply(to: str, body: str) -> str:
            """Reply to a customer."""
            self.sent.append(f"{to}: {body}")
            return "sent"

        @self.tools.tool(requires_approval=True, dry_result="refund planned")
        def issue_refund(order: str, cents: int) -> str:
            """Refund an order."""
            self.refunds.append(cents)
            return f"refunded {cents}"

    def think(self, prompt: str) -> Any:
        @traced_llm(model="demo-model", tape=self.tape)
        def call(text: str) -> Any:
            record_step("llm", "demo-model")
            return self.model.complete(messages=[{"role": "user", "content": text}])

        return call(prompt)


@pytest.fixture
def desk() -> Desk:
    return Desk()


def test_replayed_step_does_not_send_twice(desk: Desk) -> None:
    reply = json.dumps({"to": "ada@example.com", "body": "Your order ships today."})
    with desk.tools.run(run_id="ticket-1"):
        desk.think("draft a reply")
        assert desk.tools.call("send_reply", reply) == "sent"
        assert desk.tools.call("send_reply", reply) == "sent"  # replay: served, not re-run
    assert len(desk.sent) == 1


def test_dry_run_plans_writes_but_still_reads(desk: Desk) -> None:
    with RunGuard(policy=ToolPolicy(dry_run=True), max_steps=10):
        order = desk.tools.call("lookup_order", json.dumps({"order_id": "o-1"}))
        refund = desk.tools.call("issue_refund", json.dumps({"order": "o-1", "cents": 500}))
        reply = desk.tools.call("send_reply", json.dumps({"to": "a@example.com", "body": "hi"}))
    assert order == {"id": "o-1", "status": "late"}
    assert refund == "refund planned"
    assert isinstance(reply, DryRun) and reply.tool == "send_reply"
    assert desk.refunds == [] and desk.sent == []


def test_approver_allows_small_refunds_and_denies_large_ones(desk: Desk) -> None:
    def approver(call: ToolCall) -> bool:
        cents = call.arguments["cents"]
        return isinstance(cents, int) and cents < 5_000

    with RunGuard(max_steps=10, policy=ToolPolicy(approver=approver, approval_timeout=5)):
        assert desk.tools.call("issue_refund", json.dumps({"order": "a", "cents": 900})) == (
            "refunded 900"
        )
        with pytest.raises(ApprovalDenied):
            desk.tools.call("issue_refund", json.dumps({"order": "b", "cents": 90_000}))
    assert desk.refunds == [900]


def test_budget_stops_the_loop(desk: Desk) -> None:
    spend = Budget(tokens=100, per="run", name="agent-spend")

    @spend
    def budgeted(prompt: str) -> Any:
        return desk.think(prompt)

    steps = 0
    with agent_run(max_steps=50, budget=spend), pytest.raises(RunBudgetExceeded):
        while True:
            record_step("llm", "demo-model")
            budgeted("keep going")
            steps += 1
    assert steps == 3  # 40, 80, 120 tokens, then the next step is refused


def test_tape_is_redacted_and_replays_the_recorded_flow(desk: Desk) -> None:
    with desk.tools.run(run_id="ticket-2"):
        desk.think("email ada@example.com about card 4111 1111 1111 1111")
        desk.tools.call("send_reply", json.dumps({"to": "ada@example.com", "body": "ok"}))

    records = desk.tape.records()
    assert [r.kind for r in records] == ["llm", "tool"]
    dumped = json.dumps([r.input for r in records], default=str)
    assert "ada@example.com" not in dumped and "4111" not in dumped

    with replay(desk.tape) as provider:
        assert provider.llm("anything") is not None
        assert provider.tool("send_reply")(to="x", body="y") == "sent"
