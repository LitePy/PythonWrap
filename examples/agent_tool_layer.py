"""A safe tool layer for an agent: guarded run, replay-proof tools, approval and an audit tape.

A scripted fake model (``fake_llm``) drives a small support agent. The script shows that:

* ``agent_run`` bounds the loop and a real ``Budget`` stops it when tokens run out;
* a side-effecting ``tool`` is not executed twice when a step is replayed;
* a risky tool needs a human approver, which can say no;
* ``traced_llm`` / ``traced_tool`` record everything to a redacted ``AuditTape``.

Run it with ``python examples/agent_tool_layer.py``; it asserts its own results.
"""

from __future__ import annotations

import json
from typing import Any

from python_wrap import (
    ApprovalDenied,
    AuditTape,
    RunBudgetExceeded,
    RunGuard,
    ToolPolicy,
    ToolRegistry,
    agent_run,
    fake_llm,
    record_step,
    traced_llm,
    traced_tool,
)
from python_wrap.llm import Budget

tape = AuditTape()
sent: list[str] = []
refunds: list[int] = []

tools = ToolRegistry(guard=RunGuard(max_steps=20, max_repeats=3))


@tools.tool
@traced_tool("send_reply", tape=tape)
def send_reply(to: str, body: str) -> str:
    """Reply to a customer."""
    sent.append(f"{to}: {body}")
    return "sent"


@tools.tool(requires_approval=True)
@traced_tool("issue_refund", tape=tape)
def issue_refund(order: str, cents: int) -> str:
    """Refund an order."""
    refunds.append(cents)
    return f"refunded {cents}"


def approver(call: Any) -> bool:
    cents = call.arguments["cents"]
    return isinstance(cents, int) and cents < 5_000


def main() -> None:
    model = fake_llm(["ok"], default="ok", usage=(30, 10))

    @traced_llm(model="demo-model", tape=tape)
    def think(prompt: str) -> Any:
        record_step("llm", "demo-model")
        return model.complete(messages=[{"role": "user", "content": prompt}])

    # 1. Tools: a replayed step does not send twice.
    reply = json.dumps({"to": "ada@example.com", "body": "Your order ships today."})
    with tools.run(run_id="ticket-1"):
        think("draft a reply for ada@example.com")
        assert tools.call("send_reply", reply) == "sent"
        assert tools.call("send_reply", reply) == "sent"  # replay: served, not re-run
    assert len(sent) == 1

    # 2. Approval: a small refund passes, a large one is denied.
    with RunGuard(max_steps=10, policy=ToolPolicy(approver=approver, approval_timeout=5)):
        assert tools.call("issue_refund", json.dumps({"order": "o-1", "cents": 900})) == (
            "refunded 900"
        )
        try:
            tools.call("issue_refund", json.dumps({"order": "o-2", "cents": 90_000}))
        except ApprovalDenied:
            pass
        else:
            raise AssertionError("a large refund needs approval")
    assert refunds == [900]

    # 3. A real Budget stops the loop: 40 tokens per call, 100 allowed.
    spend = Budget(tokens=100, per="run", name="agent-spend")

    @spend
    def budgeted_think(prompt: str) -> Any:
        return think(prompt)

    steps = 0
    with agent_run(max_steps=50, budget=spend):
        try:
            while True:
                record_step("llm", "demo-model")  # raises RunBudgetExceeded once spent
                budgeted_think("keep going")
                steps += 1
        except RunBudgetExceeded:
            pass
    assert steps == 3, steps  # 40, 80, 120 tokens, then the next step is refused

    # 4. The tape has redacted records of everything above.
    kinds = [record.kind for record in tape.records()]
    assert kinds.count("tool") >= 2 and kinds.count("llm") == 4
    assert "ada@example.com" not in json.dumps([r.input for r in tape.records()], default=str)
    print(f"{len(tape)} calls on the tape; the agent stopped after {steps} budgeted steps")


if __name__ == "__main__":
    main()
