# Policies

A **policy** is a reusable, named bundle of decorators. You say *which* protections a call needs; the policy applies them in the [recommended order](../../get-started/stacking.md), however you list them.

```python
from python_wrap import CircuitBreaker, policy, retry, timeout

payments = policy(
    timeout=timeout(2),                                   # listed first on purpose
    retry=retry(max_attempts=3, delay=0.01, on=ConnectionError),
    breaker=CircuitBreaker(failure_threshold=5, on=ConnectionError),
    name="payments",
)

@payments
def charge(amount: int) -> int:
    return amount

assert charge(5) == 5
print(payments)   # Policy('payments', breaker > retry > timeout)
```

## When to use it

- Several calls need the same protections (every call to one service).
- You want the stacking order guaranteed instead of hand-checked.
- Not for a single call with one decorator: use the decorator directly.

## Roles

Each decorator goes into a named **role**. The role decides the position, so the code cannot be mis-ordered, and the arguments are checked by the type checker. Guessing a role from a decorator object would be fragile (factories return plain closures), so python-wrap does not guess.

Outermost first:

| Role | Typical decorators | Why here |
| --- | --- | --- |
| `observe` | `timing`, `audit`, `log_args` | See the outcome the caller gets, retries included. |
| `validate` | `validate_args`, `enforce_types` | Bad input is rejected before anything else runs. |
| `fallback` | `fallback`, `suppress` | Catches what the layers below could not fix. |
| `cache` | `memoize`, `once`, `singleflight` | A hit skips everything below. |
| `limiter` | `rate_limit`, `throttle` | One token per logical call, not per attempt. |
| `bulkhead` | `Bulkhead`, `limit_concurrency` | Caps concurrent logical calls. |
| `breaker` | `CircuitBreaker`, `circuit_breaker` | Counts calls whose retries were exhausted. |
| `retry` | `retry` | Repeats everything below. |
| `timeout` | `timeout` | A deadline per attempt. |
| `guard` | `revert_on_failure`, `transactional` | Each attempt starts from clean state. |

A role accepts one decorator or a list (outermost first). For a decorator with no role, `outer=` places it outside every role and `inner=` just above the function.

## Deriving variants

Policies are immutable. `with_()` returns a changed copy; `None` removes a role.

```python
from python_wrap import policy, retry, timeout

base = policy(retry=retry(max_attempts=3, delay=0), timeout=timeout(2), name="io")
patient = base.with_(timeout=timeout(30), name="io-patient")
no_retry = base.with_(retry=None)

assert base.order == ("retry", "timeout")
assert no_retry.order == ("timeout",)
```

## Async and methods

A policy is a decorator for plain, `async def`, instance, static and class methods, and keeps the function's type. Combine it with [`wrap_methods`](../devtools.md#wrap_methods) to protect a whole class.

```python
import asyncio
from python_wrap import policy, retry

p = policy(retry=retry(max_attempts=2, delay=0))

@p
async def load(x: int) -> int:
    return x

assert asyncio.run(load(1)) == 1
```

## Gotchas

!!! note "Shared state"
    A `CircuitBreaker` instance is shared by everything the policy decorates, which is usually what you want for one dependency. Other decorators create state per decorated function.

!!! note "`python_wrap.policy` is the function"
    The package attribute `python_wrap.policy` is the `policy` function, not the module.

## See also

- [Stacking](../../get-started/stacking.md) for the reasoning behind the order
- [Retry](retry.md), [Circuit breaker](circuit-breaker.md), [Timeout](timeout.md), [Fallback](fallback.md)
- [API reference](../../api/policy.md)
