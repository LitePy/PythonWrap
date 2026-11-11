# A safe tool layer for agents

## The problem

An agent loop calls a model, the model asks for tools, tools change the world. Things that go wrong: the orchestrator replays a turn and the customer gets two emails; the model repeats one call forever; a refund is issued without anyone agreeing; the bill for one run is unbounded; and afterwards nobody can say what the agent did.

## The code

```python
import json

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


def approver(call) -> bool:
    cents = call.arguments["cents"]
    return isinstance(cents, int) and cents < 5_000


model = fake_llm(["ok"], default="ok", usage=(30, 10))


@traced_llm(model="demo-model", tape=tape)
def think(prompt: str):
    return model.complete(messages=[{"role": "user", "content": prompt}])


# A replayed step does not run its side effect twice.
reply = json.dumps({"to": "ada@example.com", "body": "Your order ships today."})
with tools.run(run_id="ticket-1"):
    think("draft a reply for ada@example.com")
    tools.call("send_reply", reply)
    tools.call("send_reply", reply)
assert len(sent) == 1

# Risky tools need a human; anything but True denies.
with RunGuard(max_steps=10, policy=ToolPolicy(approver=approver, approval_timeout=5)):
    assert tools.call("issue_refund", json.dumps({"order": "o-1", "cents": 900})) == "refunded 900"
    try:
        tools.call("issue_refund", json.dumps({"order": "o-2", "cents": 90_000}))
    except ApprovalDenied:
        pass
assert refunds == [900]

# A real Budget bounds the run.
spend = Budget(tokens=100, per="run", name="agent-spend")


@spend
def budgeted_think(prompt: str):
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
assert steps == 3

# The tape holds redacted records of every call.
assert "ada@example.com" not in json.dumps([r.input for r in tape.records()], default=str)
```

## How it works

- `ToolRegistry` dispatches tools the way a model asks for them (name plus JSON arguments). Side-effecting tools are deduplicated within a run, so a replayed step returns the recorded result instead of running again; mark pure reads `read_only=True`.
- `RunGuard` / `agent_run` bound a run with `max_steps`, `max_seconds` and `max_repeats`. Passing a `python_wrap.llm.Budget` opens its per-run scope, so every decorated LLM call inside draws on one counter, and the next step raises `RunBudgetExceeded` once it is spent.
- `requires_approval=True` asks the policy's approver before each call. Approvers can be sync or async and wire to a chat message, ticket or CLI prompt. `ToolPolicy(dry_run=True)` plans without acting.
- `traced_llm` and `traced_tool` publish events and record to an `AuditTape` with secrets, emails and card numbers redacted. `replay(tape)` later serves recorded results in a test; it never runs tools or calls a provider.

## Pitfalls

- A decorated LLM call made directly raises `BudgetExceeded`; the run's own step check raises `RunBudgetExceeded`. Catch the one that matches where you call.
- Unbounded runs are flagged by the doctor (PW017), and retried side-effecting tools with deduplication off by PW016.
- Redaction is pattern based; add your own patterns for domain identifiers before storing a tape.

## Test it

Drive the loop with `fake_llm`, inject faults with `chaos.llm`, and assert on `tape.records()`. The runnable script is `examples/agent_tool_layer.py`; see [Agent tool safety](../features/ai/agent-safety.md) and [AI observability](../features/ai/observability.md).
