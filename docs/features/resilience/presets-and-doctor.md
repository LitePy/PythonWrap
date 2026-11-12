# Presets and the policy doctor

Two tools for getting a resilience stack right: **presets** give you sensible numbers to
start from, and the **doctor** explains any stack and flags the combinations that cause
production incidents.

## Preset policies

Choosing timeouts, attempt counts and breaker thresholds is the hard part. The presets in
`python_wrap.presets` return an ordinary `Policy` with documented budgets:

| Preset | Timeout | Attempts | Waits between attempts | Breaker | Extras |
| --- | --- | --- | --- | --- | --- |
| `http_client(name)` | 10 s | 3 | 0.2 s, 0.4 s (+ up to 0.1 s jitter) | 5 failures, 30 s | |
| `db_call(name)` | 5 s | 3, transient DB errors only | 0.05 s, 0.1 s (+ up to 0.05 s) | off (opt in) | `retry_if=db_transient` |
| `third_party_api(name)` | 15 s | 4 | 0.5, 1, 2 s (+ up to 0.5 s) | 5 failures, 60 s | optional `calls=`/`period=` limiter and `max_concurrent=` bulkhead |
| `llm_client(name)` | 60 s | 4, transient LLM errors only | 1, 2, 4 s (+ up to 0.5 s) or `Retry-After` | 5 transient failures, 60 s | optional `spend=` budget and `tokens=` limiter; see [LLM calls](../ai/llm-calls.md) |

Every retry layer carries a `RetryBudget` (20 % of traffic plus 5 retries per second) and
a `max_time` longer than `timeout * attempts`, so a slow attempt never eats the whole budget.
Every argument can be overridden.

```python
from python_wrap.presets import db_call, http_client, third_party_api

inventory = http_client("inventory", timeout_seconds=3)
orders_db = db_call("orders-db")
payments = third_party_api("payments", calls=20, period=1, max_concurrent=10)


@inventory
def stock_level(sku: str) -> int:
    return 3


@orders_db
def save_order(order_id: int) -> int:
    return order_id


assert stock_level("A-1") == 3
assert save_order(9) == 9
```

### Non-idempotent operations

A charge, an e-mail or a POST without an idempotency key must not run twice. Pass
`idempotent=False` and the preset leaves the retry layer out (timeouts, breakers, limits
and bulkheads stay):

```python
from python_wrap.presets import third_party_api

charge_policy = third_party_api("payments", idempotent=False)
assert "retry" not in [layer.role for layer in charge_policy.explain().layers]
```

## Explaining a policy

`Policy.explain()` (or `python_wrap.doctor.explain`) lists the layers outermost first with
their settings and returns findings:

```python
from python_wrap import RetryBudget, policy, retry, timeout

stack = policy(
    name="catalog",
    retry=retry(max_attempts=3, on=ConnectionError, budget=RetryBudget()),
    timeout=timeout(2),
)
report = stack.explain()
print(report)
assert report.ok and report.findings == ()
```

`doctor(func)` does the same for a function decorated by hand, by walking its
`__wrapped__` chain:

```python
from python_wrap import retry, timeout
from python_wrap.doctor import doctor


@timeout(5)
@retry(max_attempts=3, on=ConnectionError)
def fetch() -> str:
    return "ok"


report = doctor(fetch)
assert [layer.role for layer in report.layers] == ["timeout", "retry"]
assert "PW006" in [finding.code for finding in report.findings]  # timeout outside retry
```

### Findings

The doctor reports codes PW001 to PW019 (the table lists them by group, not strictly in numeric order).

| Code | Severity | Meaning |
| --- | --- | --- |
| PW001 | error | Retry with neither `max_attempts` nor `max_time`: it retries forever. |
| PW002 | error | Retry around an operation declared non-idempotent (`idempotent=False`). |
| PW003 | warning | Retry catches every `Exception` without `retry_if`: bugs are retried too. |
| PW004 | warning | Retry without a per-attempt timeout: one hung attempt blocks forever. |
| PW005 | warning | The per-attempt timeout is not shorter than the retry `max_time`. |
| PW006 | warning | Layers out of the canonical order (`ROLES`), e.g. a retry wrapping a breaker. |
| PW007 | warning | More than one retry or breaker layer: attempts multiply. |
| PW008 | warning | Retry without a `RetryBudget`: retries multiply load during an outage. |
| PW009 | warning | A sync `timeout` cannot stop the function here (non-main thread, no `SIGALRM`, `use_signals=False`): side effects may continue. Poll `checkpoint()`. |
| PW010 | warning | `timeout` would take over an existing `SIGALRM` handler or armed timer. |
| PW011 | warning | `memoize` on a method with `self` in the key and no `maxsize`, `ttl` or `store`: instances are pinned forever. Use `per_instance=True`. |
| PW012 | warning | With `multiprocess=True`: a breaker, bulkhead, rate limiter or adaptive limiter without a shared backend, so every worker process enforces its own copy of the limit. |
| PW016 | error | An agent `tool` with side effects is retried (its own `retry=` or an outer `retry`) but deduplication is off (`idempotent_key=False`): a retried or replayed call runs twice. See [Agent tool safety](../ai/agent-safety.md). |
| PW017 | warning | An agent run (`RunGuard`, `agent_run`, or a `ToolRegistry` without a guard) sets none of `max_steps`, `max_seconds`, `max_repeats`: a looping model never stops. |
| PW013 | warning | An LLM call is retried but has no spend `Budget`. See [LLM calls](../ai/llm-calls.md#doctor-checks). |
| PW014 | warning | A stack with LLM layers has no timeout, or a `stream_timeout` has no `chunk_gap`. |
| PW015 | warning | A generic `retry` without `retry_if=` wraps an LLM call: refusals and bad requests are retried. |
| PW020 | warning | An `mcp_guard` (or a pydantic-ai / OpenAI Agents SDK tool wrapped by the python-wrap adapters) has no timeout: a hung server or tool holds the agent turn forever. See [Agent frameworks and MCP](../ai/agent-frameworks.md#doctor-checks). |
| PW021 | warning | An `mcp_guard` retries every tool (`retry=` without `retry_tools=`): a retried MCP write may run twice. |
| PW018 | warning | A python-wrap LLM retry wraps an SDK client (`llm_retry(client=...)`) whose own retries are still on: attempts multiply (4 x 3 = 12). See [OpenAI and Anthropic SDK clients](../ai/openai-anthropic.md#doctor-checks). |
| PW019 | warning | A retry wraps a streamed LLM call (`stream_timeout`) without `resume_safe`: a replay after output started repeats text and half-done tool calls. |

Breakers, bulkheads and limiters keep their state in the process by default. Under a
process pool (gunicorn or uvicorn workers, Celery prefork, `ProcessPoolExecutor`) that
means N workers allow N times the configured limit, and each worker learns about an
outage separately. Tell the doctor the code runs that way to get PW012:

```python
from python_wrap import CircuitBreaker, policy, timeout
from python_wrap.doctor import explain

stack = policy(breaker=CircuitBreaker(name="vendor"), timeout=timeout(2))
report = explain(stack, multiprocess=True)
assert [finding.code for finding in report.findings] == ["PW012"]
```

Give the layer a shared backend (`RedisBreakerState`, `SQLiteBreakerState`, a lease
backend for bulkheads, a Redis window for limiters) or size the limit per worker on
purpose.

Use it in a test so a risky change fails CI:

```python
from python_wrap.presets import http_client


def test_resilience_stacks_are_safe() -> None:
    assert http_client("users").explain().ok


test_resilience_stacks_are_safe()
```

## Command line

<!-- skip -->
```bash
python -m python_wrap doctor myapp.clients:users_policy myapp.jobs:sync_orders
python -m python_wrap doctor --non-idempotent myapp.payments:charge
python -m python_wrap doctor --strict myapp.clients:users_policy   # warnings fail too
python -m python_wrap doctor --multiprocess myapp.clients:users_policy  # PW012 checks
```

The exit status is 1 when any report has an error (or any finding with `--strict`).
