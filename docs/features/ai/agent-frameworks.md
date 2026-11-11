# Agent frameworks and MCP

Agent frameworks run the model loop for you; python-wrap adds what they leave to you:
**side effects that survive replays and crashes**, **one spend budget across stacks**, an
**audit tape**, **loop and tool-call limits that cover sub-agents**, and **timeouts,
retries and breakers around MCP servers**. Nothing here imports pydantic-ai, the OpenAI
Agents SDK or an MCP SDK at module level, and python-wrap still has no dependencies.

| Piece | Where | What it does |
| --- | --- | --- |
| `EffectLedger` | `python_wrap` | Records every side effect as started / succeeded / failed, keyed on the provider's tool-call id. |
| `PythonWrapCapability` | `python_wrap.adapters.pydantic_ai` | Guards pydantic-ai tools, bridges run usage into a `Budget`, writes an `AuditTape`. |
| `AgentsGuard` | `python_wrap.adapters.openai_agents` | Guarded `function_tool`s, `FunctionTool` wrappers (sub-agents too) and `RunHooks`. |
| `mcp_guard` | `python_wrap` | Timeout, selective retry and a per-server breaker around MCP `call_tool`. |

Install the extras you use (they only pull in the framework itself):

```bash
pip install "python-wrap[pydantic-ai]"     # or: "python-wrap[openai-agents]"
```

## The effect ledger

`@tool` already replays a write tool's result when the same arguments come back in the
same run. That is not enough once a framework is involved: the framework retries a step
with the **same tool-call id**, while the model may legitimately ask for the **same
action twice** with two different ids, and the process can die between the side effect
and the moment its result is stored.

`EffectLedger` keys each call on the provider's tool-call id (OpenAI `tool_call.id`,
Anthropic `tool_use.id`, pydantic-ai `tool_call_id`) when one is current, and falls back
to the run id plus the argument digest otherwise. Each call moves through three states:

```
claim (started) -> run the tool -> succeeded (result stored) | failed (error stored)
```

```python
from python_wrap import EffectLedger, agent_run, tool, with_call_id

ledger = EffectLedger()
charges = []

@tool(ledger=ledger)
def charge(order: str, cents: int) -> str:
    """Charge an order."""
    charges.append(order)
    return f"charged {order}"

with agent_run():
    with with_call_id("call_1"):
        charge("A-1", 500)
        charge("A-1", 500)       # the framework replays call_1: stored result
    with with_call_id("call_2"):
        charge("A-1", 500)       # the model asked again: a new call, it runs
assert charges == ["A-1", "A-1"]
```

The adapters set the call id for you; when you dispatch a model's tool calls yourself,
wrap each one in `with with_call_id(block.id):`. Reusing an id with different arguments
raises `EffectConflict`. An `idempotent_key=` callable still wins over the call id, for
business keys such as an order number.

Guarantees:

- **Replays** of a call id return the stored result without running the tool.
- **Concurrent duplicates** (threads, tasks or processes sharing a store) run the tool
  once; the others wait for it and get its result.
- **Failures** are recorded; an ordinary exception lets the next call run again.
  pydantic-ai's `ModelRetry` is always an ordinary failure.

### Crashes and the decision hook

A `started` record that nobody finished means the side effect **may or may not** have
happened: the process died, or the ledger store failed right after the effect. The
ledger never re-runs such a call blindly. The record is detected once its `lease`
expires (at once in the process that saw the store fail), and so are failures that may
have left the effect half done (`TimeoutError`, cancellation). The `on_unknown` hook
decides:

| Hook returns | Effect |
| --- | --- |
| `"retry"` | Run the tool again (it is safe, or you checked it did not happen). |
| `EffectResolved(value)` | Record `value` as the result (you looked it up) and return it. |
| `"raise"` or `None` (or no hook) | Raise `EffectUnknown` with the record and a `reason`. |

```python
from python_wrap import (
    EffectLedger, EffectResolved, EffectUnknown, MemoryEffectStore, tool, with_call_id,
)

class FlakyStore(MemoryEffectStore):
    fail_next = False

    def settle(self, record, ttl, /):
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("store unreachable")
        return super().settle(record, ttl)

bank = []
store = FlakyStore()

def reconcile(unknown: EffectUnknown):
    assert unknown.reason == "crashed"
    return EffectResolved("charged") if bank else "retry"

@tool(ledger=EffectLedger(store, on_unknown=reconcile))
def pay(order: str) -> str:
    bank.append(order)
    return "charged"

with with_call_id("call_7"):
    store.fail_next = True
    try:
        pay("B-2")
    except ConnectionError:
        pass                      # the payment happened, the record did not
    assert pay("B-2") == "charged"
assert bank == ["B-2"]            # never charged twice
```

### Stores

| `store=` | Scope |
| --- | --- |
| `None` / `MemoryEffectStore()` | One process. |
| a path / `SQLiteEffectStore(path)` | Every process on the host; survives restarts. |
| `RedisEffectStore(client)` or a `RedisStore(..., fail_open=False)` | Every worker; claims and outcomes are Lua scripts, so they are atomic. |

Only the attempt that owns a record can write its outcome: a slow attempt whose lease
was taken over cannot overwrite the newer one. SQLite and Redis serialise records as JSON
by default (pass `dumps=` / `loads=` for other types); a result JSON cannot encode is not
stored, and replaying it raises `EffectUnknown(reason="result_lost")` unless the hook
resolves it. Set `lease=` longer than the tool's slowest call, and `wait_timeout=` to
bound how long a duplicate waits for an in-flight call.

## pydantic-ai

`PythonWrapCapability` reaches pydantic-ai two ways, both keeping the tool's signature
and docstring so pydantic-ai builds the same schema:

- `cap.tool` wraps one function for `agent.tool` / `agent.tool_plain`. A first
  `ctx: RunContext` parameter supplies the `tool_call_id` and the running usage, and is
  left out of the argument digest.
- `cap.wrap(toolset)` returns a pydantic-ai `WrapperToolset` whose `call_tool` goes
  through the capability; `cap.toolset(f1, f2)` builds one from plain functions.

<!-- skip -->
```python
from pydantic_ai import Agent, RunContext
from python_wrap import AuditTape, Budget, EffectLedger, RunGuard, retry
from python_wrap.adapters.pydantic_ai import PythonWrapCapability

cap = PythonWrapCapability(
    timeout=20,
    retry=retry(max_attempts=3, on=ConnectionError),   # ModelRetry is never retried
    ledger=EffectLedger("effects.db"),
    approval=["refund_*"],
    max_tool_calls=30,
    max_repeats=3,
    guard=RunGuard(max_seconds="5m"),
    budget=Budget(usd=2.0, per="key", key=lambda: "tenant-a", prices={"*": (3.0, 15.0)}),
    tape=AuditTape("agent-tape.jsonl"),
)

def refund_order(ctx: RunContext[None], order_id: str) -> str:
    """Refund an order."""
    return f"refunded {order_id}"

agent = Agent("openai:gpt-4o", toolsets=[cap.toolset(refund_order)])
result = cap.run_sync(agent, "Refund order 42")
```

Every tool call then counts against `max_tool_calls`, `max_repeats` and the current
`RunGuard` (an identical call repeated too often ends the run with `LoopDetected`), passes
the `ToolPolicy` and approval checks, is deduplicated by the ledger on `tool_call_id`,
runs under a per-attempt `timeout` and `retry`, and is written to the tape. `ModelRetry`
passes through the retry untouched: it is pydantic-ai's way of asking the model to try
again, so it reaches pydantic-ai as is. A refused tool ends the run (`ToolNotAllowed`), or
with `on_denied="retry"` is reported to the model as a `ModelRetry`.

### One budget, two kinds of limits

`RunContext.usage` and the result's `usage()` are running totals; the capability adds
only their growth to the python-wrap `Budget` (at each tool call, and once more with the
final total in `run` / `run_sync` or `cap.finish(result)`), and refuses the next tool call
once the budget is spent.

- Keep pydantic-ai's **`UsageLimits`** for caps on one run's requests and tokens: it is
  enforced inside the model loop, before each request.
- Use a python-wrap **`Budget`** for dollars, per-user or per-tenant limits, limits that
  span many runs and processes (`RedisBudgetBackend`), and one limit across pydantic-ai,
  the OpenAI Agents SDK and direct SDK calls.

Use one capability per concurrent run when a budget is attached, or call `cap.reset()`
between runs if you do not use `run` / `run_sync`.

## OpenAI Agents SDK

`AgentsGuard` gives guarded function tools, wraps existing `FunctionTool`s and supplies
hooks:

<!-- skip -->
```python
from agents import Agent, Runner
from python_wrap import AuditTape, Budget, EffectLedger
from python_wrap.adapters.openai_agents import AgentsGuard

guard = AgentsGuard(
    timeout=20, ledger=EffectLedger(), max_tool_calls=40, max_repeats=3,
    budget=Budget(tokens=200_000), tape=AuditTape("agents-tape.jsonl"),
)

@guard.function_tool
def create_ticket(title: str) -> str:
    """Open a support ticket."""
    return "T-1001"

researcher = Agent(name="researcher", instructions="...", hooks=guard.agent_hooks())
research = researcher.as_tool("research", "Look things up", hooks=guard.hooks())
triage = Agent(name="triage", tools=[create_ticket, guard.wrap(research)])
result = Runner.run_sync(triage, "Customer cannot log in", hooks=guard.hooks())
```

- `guard.function_tool` is `agents.function_tool` with python-wrap `tool` semantics
  inside (timeout, retry, `ToolPolicy`, approval, ledger keyed on the SDK's
  `tool_call_id`). Extra keyword arguments go to `agents.function_tool`. The SDK's own
  `timeout_seconds=` also counts as a timeout for PW020.
- `guard.wrap(tool)` puts the limits, ledger, timeout and tape around any `FunctionTool`,
  including the one `Agent.as_tool()` returns. A sub-agent run as a tool has its own
  `max_turns` and nothing bounds the tree as a whole; the guard's counters are shared by
  every wrapped tool at any depth, so one `max_tool_calls` caps them all.
- `guard.hooks()` (for `Runner.run`) and `guard.agent_hooks()` (for `Agent(hooks=...)`,
  useful on sub-agents) feed each response's usage into the budget, refuse the next model
  call once it is spent, record tool and model calls on the tape, and count tools that
  are not wrapped.

The SDK turns a tool exception into a message for the model. Errors that should end the
run (`LoopDetected`, `StepLimitExceeded`, a spent budget, `EffectUnknown`,
`EffectConflict`) are re-raised by the wrapper anyway; a refused tool or a tool timeout
still goes to the model as a tool error. Change the set with `fatal=`.

## MCP servers

`mcp_guard` wraps an MCP `ClientSession` (any object with `call_tool`) or a bare
`call_tool(name, arguments)` function, sync or async:

```python
import asyncio
from python_wrap import CircuitOpenError, mcp_guard, retry

class Session:                        # stands in for mcp.ClientSession
    name = "files"
    calls = 0

    async def call_tool(self, name, arguments=None):
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("server restarting")
        return {"content": [{"type": "text", "text": "hello"}], "isError": False}

async def main():
    session = mcp_guard(
        Session(),
        timeout=10,                                   # per attempt
        retry=retry(max_attempts=3, delay=0, on=ConnectionError),
        retry_tools=["read_*", "list_*"],             # never retry writes blindly
        breaker={"failure_threshold": 5, "recovery_timeout": 30},
    )
    reply = await session.call_tool("read_file", {"path": "notes.txt"})
    assert reply["content"][0]["text"] == "hello"

asyncio.run(main())
```

- **Timeout** per attempt (`timeout=None` disables it and the doctor reports PW020).
- **Retry** only for the tools in `retry_tools` (globs or a predicate): MCP tools can
  write, so a blanket retry (PW021) may run a write twice. `retry=3` is shorthand for
  three attempts on `ConnectionError`, `TimeoutError` and `OSError` with backoff.
- **One breaker per server name** (`mcp_breaker(name)`), shared by every guard for that
  server, so a dead server is noticed once per process. A result with `isError` set is
  the tool's answer, not a server failure: it is returned and does not trip the breaker.
- **Cooperative cancel**: the current `CancelToken` (an enclosing `timeout`, or a
  `RunGuard` that ran out of time or was cancelled) is checked before every attempt, and
  an in-flight async call is cancelled as soon as it fires.

The guarded object forwards everything else (`list_tools`, `initialize`...) unchanged.

## Doctor checks

- **PW020** (warning): an `mcp_guard`, or a pydantic-ai / OpenAI Agents SDK tool wrapped
  by these adapters, has no timeout.
- **PW021** (warning): an `mcp_guard` retries every tool (`retry=` without `retry_tools=`).

```python
from python_wrap import mcp_guard
from python_wrap.doctor import doctor

def call_tool(name, arguments=None):
    return {"content": [], "isError": False}

risky = mcp_guard(call_tool, server="search", timeout=None, retry=3)
codes = [finding.code for finding in doctor(risky).findings]
assert "PW020" in codes and "PW021" in codes
```

## Limits

- The ledger guarantees at most one *recorded* execution per key; exactly-once effects
  in an external system still need that system's idempotency key (pass the call id or
  your business key along) or the decision hook to check it.
- Counters (`max_tool_calls`, `max_repeats`) and the usage bridge belong to the
  capability / guard object: use one per concurrent run, or a `RunGuard` per run.
- The adapters are tested against pydantic-ai 1.x and openai-agents 0.x through their
  public toolset, tool and hook interfaces; a framework change to those interfaces may
  need an adapter update.

See also [Agent tool safety](agent-safety.md), [AI observability and audit](observability.md)
and the runnable [`examples/sdk_adapters/framework_tools.py`](https://github.com/LitePy/PythonWrap/blob/main/examples/sdk_adapters/framework_tools.py).
