# Agent tool safety

When a model drives a tool-calling loop, four things go wrong in practice:

- a retried or replayed "send email" step **runs twice**;
- the loop **never ends**, because the model keeps calling the same tool with the same arguments;
- a tool with **side effects runs** when it should only have been planned;
- an irreversible action runs **without a human saying yes**.

`python_wrap.agents` handles all four without an agent framework or a provider SDK. It works with sync and `async def` code.

```python
from python_wrap import ToolPolicy, agent_run, tool

sent = []

@tool
def send_email(to: str, subject: str) -> str:
    """Send an email."""
    sent.append(to)
    return f"queued for {to}"

@tool(read_only=True)
def search(query: str) -> list[str]:
    """Search the knowledge base."""
    return [query.upper()]

with agent_run(max_steps=20, max_seconds="2m", max_repeats=3) as run:
    search("refund policy")
    send_email("ada@example.com", "Your refund")
    send_email("ada@example.com", "Your refund")   # replayed: not sent again
assert sent == ["ada@example.com"]
assert run.steps == 3
```

## Idempotent tools

`@tool` marks a function as an agent tool. A tool with side effects (the default, `read_only=False`) gets an **idempotency key** built from the tool name, the canonical arguments (keyword order does not matter, defaults are applied) and the current run id. A second call with the same key within `ttl` returns the stored result **without running** and emits `tool.replayed`. Only successful results are stored: a call that raised runs again next time. A concurrent duplicate waits for the first call and returns its result.

| `idempotent_key=` | Effect |
| --- | --- |
| `None` (default) | Derived key for write tools; read-only tools are not deduplicated. |
| `True` | Derived key for read-only tools too. |
| `False` | No deduplication (the doctor flags this with a retry: PW016). |
| a callable | Called with the tool's arguments; its result is the key, not scoped to the run. Use it for business keys such as an order id. |

Because the derived key includes the run id, resuming a run with the same `run_id=` replays the calls it already made:

```python
from python_wrap import agent_run, tool

charged = []

@tool
def charge(order_id: str, cents: int) -> str:
    charged.append(order_id)
    return f"charged {order_id}"

with agent_run(run_id="order-42-attempt"):
    charge("o-42", 1999)
# ... the process restarts, the orchestrator resumes the same run ...
with agent_run(run_id="order-42-attempt"):
    assert charge("o-42", 1999) == "charged o-42"
assert charged == ["o-42"]
```

The default store is an in-process `MemoryResultStore`. Pass `store=RedisStore(client, fail_open=False)` to deduplicate across processes; see [Leases and idempotency](../concurrency/leases-and-idempotency.md) for the guarantees and their limits.

`retry=` and `timeout=` are applied **inside** the deduplication, so a replay never re-runs them:

```python
from python_wrap import retry, tool

attempts = []

@tool(retry=retry(max_attempts=3, delay=0, on=ConnectionError), timeout=10)
def post_comment(ticket: str, body: str) -> str:
    attempts.append(ticket)
    if len(attempts) == 1:
        raise ConnectionError("reset")
    return "posted"

assert post_comment("T-1", "Thanks!") == "posted"
assert post_comment("T-1", "Thanks!") == "posted"   # replayed
assert len(attempts) == 2
```

`tool_spec(func)` returns the recorded `ToolSpec` (name, description, `read_only`, `idempotent`, `requires_approval`, `timeout`, `retried`, `is_async`).

## Run guard

`RunGuard` (or the `agent_run(...)` shorthand) bounds one agent run. Enter it with `with` or `async with`, or use it as a decorator on the loop function; each entry starts a new `AgentRun`.

| Limit | Raises |
| --- | --- |
| `max_steps` | `StepLimitExceeded` on the step after the limit. Tool calls count automatically; count LLM calls with `record_step("llm", model)`. |
| `max_seconds` | `RunTimeout` (also a `TimeoutExpired` and `TimeoutError`) on the next step once the time is up. |
| `max_repeats` | `LoopDetected` when one identical call (same kind, name and arguments) was already made that many times. |
| `budget` | Checked before each step. A `python_wrap.llm.Budget` opens its per-run scope for the run (see below); any other object has its `check()` called if it has one, and `exhausted == True` raises `RunBudgetExceeded`. |

### Using an LLM `Budget`

Pass a real `python_wrap.llm.Budget` and entering the run opens that budget's run scope (`budget.run()`), closed on exit. Every LLM call decorated with it inside the run draws on one fresh counter (`per="run"` or `"chain"`; a `per="total"` budget is checked as it stands). Once the counter is used up, the next step raises `RunBudgetExceeded`; a decorated call made directly still raises `llm.BudgetExceeded`.

```python
from python_wrap import RunBudgetExceeded, agent_run, record_step
from python_wrap.llm import Budget

spend = Budget(tokens=100, per="run")

@spend
def ask(prompt: str) -> dict:
    return {"usage": {"input_tokens": 80, "output_tokens": 40}}

with agent_run(budget=spend):
    ask("plan")  # crosses the limit; the paid-for result is returned
    try:
        record_step("llm", "model")
    except RunBudgetExceeded:
        print("run budget used up")
```

```python
from python_wrap import LoopDetected, agent_run, record_step, tool

@tool(read_only=True)
def get_weather(city: str) -> str:
    return "sunny"

def model_turn() -> str:
    record_step("llm", "my-model")       # counts against max_steps
    return get_weather("Cairo")          # the model asks the same thing every time

with agent_run(max_steps=50, max_repeats=3) as run:
    try:
        while True:
            model_turn()
    except LoopDetected as error:
        stopped = error
assert stopped.name == "get_weather" and stopped.repeats == 3
assert [step.kind for step in run.history][:2] == ["llm", "tool"]
```

`current_run()` returns the innermost `AgentRun` (or `None`): `run_id`, `steps`, `history` (a tuple of `Step`), `elapsed`, `remaining`, `cancel()` and `check()`. The run id is also published in the context variable `python_wrap.agents.run_id_var`, which tracing integrations can read.

Runs **nest**: a sub-agent's steps count against the enclosing run's limits too, so it cannot get around them. Policies of all enclosing runs apply.

### Cooperative stop

Each run installs a `CancelToken` as the current token. When `max_seconds` pass (a timer fires) or `run.cancel()` is called, the token is cancelled, so long-running tool code that calls [`checkpoint()`](../resilience/timeout.md#cooperative-cancellation) stops at its next checkpoint:

```python
import time
from python_wrap import TimeoutExpired, agent_run, checkpoint, tool

@tool(read_only=True)
def crawl(pages: int) -> int:
    done = 0
    for _ in range(pages):
        checkpoint()            # raises once the run is out of time
        time.sleep(0.01)
        done += 1
    return done

try:
    with agent_run(max_seconds=0.05):
        crawl(1000)
except TimeoutExpired:
    pass
else:
    raise AssertionError("the crawl should have stopped")
```

A token also reports cancelled when an enclosing `timeout` fires.

## Permissions and dry runs

`ToolPolicy` decides which tools a run may call. Attach it with `agent_run(policy=...)` or to a `ToolRegistry`. Every policy in effect must agree; the strictest answer wins.

| Field | Effect |
| --- | --- |
| `allow=["read_*", "search"]` | Only matching names (`fnmatch` globs) run; `None` allows all. |
| `deny=["delete_*"]` | Matching names never run; deny wins over allow. |
| `allow_write=False` | Tools not declared `read_only=True` raise `ToolNotAllowed` (reason `"read_only"`). |
| `dry_run=True` | Write tools return their `dry_result=` (a value, or a callable given the tool's arguments) or a falsy `DryRun(tool, arguments)` marker instead of running. Read-only tools still run. |

```python
from python_wrap import DryRun, ToolNotAllowed, ToolPolicy, agent_run, tool

@tool(dry_result="would refund")
def refund(order_id: str) -> str:
    raise AssertionError("not in a dry run")

@tool
def delete_account(user_id: str) -> None:
    raise AssertionError("never")

with agent_run(policy=ToolPolicy(dry_run=True, deny=["delete_*"])):
    assert refund("o-1") == "would refund"
    try:
        delete_account("u-1")
    except ToolNotAllowed as error:
        assert error.reason == "denied"
```

## Human approval

`requires_approval=True` asks the approver of the policy in effect before every call. The approver is any sync or `async` callable that receives a `ToolCall` (`tool`, `arguments`, `run_id`, `read_only`, `description`) and must return exactly `True`. Everything else denies: `False`, a truthy non-`True` value, an exception (`ApprovalDenied`, with the error as `__cause__`), no answer within `approval_timeout` seconds (`ApprovalTimeout`), or no approver at all. There is no UI dependency: wire the approver to a chat message, a ticket or a CLI prompt.

```python
from python_wrap import ApprovalDenied, ToolCall, ToolPolicy, agent_run, tool

def approver(call: ToolCall) -> bool:
    return call.arguments["cents"] < 10_000      # auto-approve small refunds only

@tool(requires_approval=True)
def refund(order_id: str, cents: int) -> str:
    return f"refunded {cents}"

with agent_run(policy=ToolPolicy(approver=approver, approval_timeout=30)):
    assert refund("o-1", 500) == "refunded 500"
    try:
        refund("o-2", 50_000)
    except ApprovalDenied:
        pass
```

A sync approver runs in a helper thread (so the timeout holds even if it blocks); in async tools a sync approver runs through `asyncio.to_thread`.

## Tool registry

`ToolRegistry` keeps tools by name and dispatches calls the way a model emits them: a name and a JSON object (or a mapping). `call` on an `async def` tool returns a coroutine. Unknown names raise `ToolNotAllowed` (reason `"unknown"`). The registry's `policy` applies to every `call`, and `registry.run()` starts a run of its `guard` with that policy in effect.

```python
from python_wrap import RunGuard, ToolPolicy, ToolRegistry

tools = ToolRegistry(policy=ToolPolicy(allow_write=False), guard=RunGuard(max_steps=10))

@tools.tool(read_only=True)
def stock(sku: str) -> int:
    """Units in stock."""
    return 3

with tools.run() as run:
    assert tools.call("stock", '{"sku": "A-1"}') == 3
assert run.steps == 1
assert [spec.name for spec in tools.specs()] == ["stock"]
```

## Events

| Event | `function` | Fields |
| --- | --- | --- |
| `agent.run.start` | guard name | `run_id`, `parent_id` |
| `agent.run.step` | guard name | `run_id`, `step`, `kind`, `tool` |
| `agent.run.end` | guard name | `run_id`, `steps`, `seconds`, `error` (exception class name or `None`) |
| `tool.call` | tool name | `run_id` |
| `tool.replayed` | tool name | `run_id` |
| `tool.dry_run` | tool name | `run_id` |
| `tool.denied` | tool name | `run_id`, `reason` |

## Doctor checks

`python_wrap.doctor` knows about tools and runs:

- **PW016** (error): a write tool is retried (its own `retry=` or an outer `@retry`) but deduplication is off (`idempotent_key=False`).
- **PW017** (warning): a `RunGuard`, an `@agent_run` loop, or a `ToolRegistry` without a guard sets none of `max_steps`, `max_seconds`, `max_repeats`.

```python
from python_wrap import RunGuard, ToolRegistry, retry, tool
from python_wrap.doctor import doctor, explain

@tool(idempotent_key=False, retry=retry(max_attempts=3, on=ConnectionError))
def send_sms(to: str) -> None: ...

assert "PW016" in [f.code for f in doctor(send_sms).findings]
assert "PW017" in [f.code for f in explain(ToolRegistry([send_sms])).findings]
assert "PW017" not in [f.code for f in explain(RunGuard(max_steps=10)).findings]
```

## Limits

- Deduplication cannot make an external effect exactly-once across a crash between the effect and storing its result; use the provider's own idempotency key where one exists (pass it through a key callable).
- `max_seconds` stops a run cooperatively: steps are refused and `checkpoint()` raises, but code that never checks keeps running. Wrap the tool in `timeout=` for a hard per-call limit.
- The default stores are per process. Share a `RedisStore` between workers for cross-process replays.
