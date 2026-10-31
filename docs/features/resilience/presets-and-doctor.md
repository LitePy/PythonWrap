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
```

The exit status is 1 when any report has an error (or any finding with `--strict`).
