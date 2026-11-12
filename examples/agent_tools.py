"""Make an agent's tool use safe: replay-proof side effects, loop limits, approvals.

A fake "model" drives a tool loop for a support desk. The script shows that:

* a replayed ``send_reply`` step (the orchestrator retried the turn) is not sent twice;
* a model stuck asking the same question is stopped by ``max_repeats``;
* a dry run plans the refund without issuing it, and read-only tools still run;
* a large refund needs a human approver, which says no;
* every step is visible as events and in ``current_run().history``.

No network, no provider SDK: the model is a list of scripted tool calls.
"""

from __future__ import annotations

import json

from python_wrap import (
    ApprovalDenied,
    DryRun,
    LoopDetected,
    RunGuard,
    ToolCall,
    ToolPolicy,
    ToolRegistry,
    current_run,
    record_step,
)
from python_wrap.doctor import explain
from python_wrap.events import listening

outbox: list[str] = []
refunds: list[int] = []

tools = ToolRegistry(guard=RunGuard(max_steps=20, max_seconds=30, max_repeats=2))


@tools.tool(read_only=True)
def lookup_order(order_id: str) -> dict[str, object]:
    """Fetch an order."""
    return {"id": order_id, "total_cents": 4_500, "status": "late"}


@tools.tool
def send_reply(ticket: str, body: str) -> str:
    """Reply to the customer."""
    outbox.append(f"{ticket}: {body}")
    return f"reply #{len(outbox)}"


@tools.tool(requires_approval=True, dry_result="refund planned")
def issue_refund(order_id: str, cents: int) -> str:
    """Refund an order."""
    refunds.append(cents)
    return f"refunded {cents}"


def fake_model(script: list[tuple[str, dict[str, object]]]) -> list[object]:
    """Play scripted tool calls, as a model's tool-use responses would."""
    results = []
    for name, arguments in script:
        record_step("llm", "fake-model")
        results.append(tools.call(name, json.dumps(arguments)))
    return results


def human(call: ToolCall) -> bool:
    """Approve refunds under 10 000 cents only."""
    cents = call.arguments["cents"]
    return isinstance(cents, int) and cents < 10_000


def main() -> None:
    # 1. A replayed turn does not send the reply twice.
    events = []
    reply = ("send_reply", {"ticket": "T-7", "body": "Sorry, it ships today."})
    with listening(events.append), tools.run(run_id="ticket-T-7") as run:
        first = fake_model([("lookup_order", {"order_id": "o-1"}), reply])
        replay = fake_model([reply])  # the orchestrator retried the last turn
    assert first[1] == replay[0] == "reply #1"
    assert outbox == ["T-7: Sorry, it ships today."]
    assert [e.name for e in events].count("tool.replayed") == 1
    print(f"run {run.run_id}: {run.steps} steps, reply sent once")

    # 2. A model stuck in a loop is stopped.
    with tools.run() as run:
        try:
            fake_model([("lookup_order", {"order_id": "o-2"})] * 10)
        except LoopDetected as error:
            print(f"stopped a loop on {error.name!r} after {error.repeats} identical calls")
        else:
            raise AssertionError("the loop should have been detected")
    assert sum(step.kind == "tool" for step in run.history) == 2

    # 3. Dry run: plan the refund without issuing it; reads still happen.
    with RunGuard(policy=ToolPolicy(dry_run=True), max_steps=10):
        planned = fake_model(
            [
                ("lookup_order", {"order_id": "o-3"}),
                ("issue_refund", {"order_id": "o-3", "cents": 4_500}),
                ("send_reply", {"ticket": "T-8", "body": "Refund on its way."}),
            ]
        )
        assert current_run() is not None
    assert planned[1] == "refund planned"
    assert isinstance(planned[2], DryRun) and planned[2].tool == "send_reply"
    assert refunds == [] and len(outbox) == 1
    print("dry run planned:", planned[1], "and", planned[2].tool)

    # 4. Approval: small refunds pass, a large one is denied.
    with RunGuard(max_steps=10, policy=ToolPolicy(approver=human, approval_timeout=5)):
        assert fake_model([("issue_refund", {"order_id": "o-4", "cents": 900})]) == ["refunded 900"]
        try:
            fake_model([("issue_refund", {"order_id": "o-5", "cents": 90_000})])
        except ApprovalDenied as error:
            print(f"refund denied ({error.reason})")
        else:
            raise AssertionError("a large refund needs approval")
    assert refunds == [900]

    # 5. The doctor is happy with this registry (it has limits).
    report = explain(tools)
    assert "PW017" not in [f.code for f in report.findings]
    print("doctor:", "ok" if report.ok else report)


if __name__ == "__main__":
    main()
