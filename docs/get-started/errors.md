# Errors

Every exception python-wrap raises on its own behalf derives from one base class, `PythonWrapError`. Where a built-in exception fits, the class also subclasses it, so code written with `except TimeoutError` or `except ValueError` keeps working.

All of them are importable from `python_wrap`.

## The hierarchy

```text
Exception
└── PythonWrapError
    ├── TimeoutExpired        (also TimeoutError)
    ├── RateLimitExceeded
    ├── CircuitOpenError
    ├── BulkheadFull
    ├── ValidationError       (also ValueError)
    ├── ArgumentTypeError     (also TypeError)
    └── BatchError
```

| Exception | Also a | Attributes | Raised when |
| --- | --- | --- | --- |
| `TimeoutExpired` | `TimeoutError` | `seconds`: the limit that was exceeded | A call or block ran longer than its limit |
| `RateLimitExceeded` | none | `retry_after`: seconds until a call would be allowed | A rate limit is used up and the call is not allowed to wait |
| `CircuitOpenError` | none | `retry_after`: seconds until the breaker tries again | The breaker is open, so the call was not attempted |
| `BulkheadFull` | none | `name`: the bulkhead's name or `None`; `max_concurrent`: its capacity | The wait queue is full, or waiting for a slot timed out |
| `ValidationError` | `ValueError` | `parameter`: the argument name; `value`: the rejected value | An argument failed a validator |
| `ArgumentTypeError` | `TypeError` | `parameter`, `expected`, `value` | An argument does not match its expected type |
| `BatchError` | none | none | A batch function broke its output contract for one caller's item |

## Which decorator raises which

| Exception | Raised by |
| --- | --- |
| `TimeoutExpired` | [`timeout`](../features/resilience/timeout.md) (decorator and context manager), [`deadline`](../features/resilience/timeout.md) when `check()` finds the budget spent or a `timeout` starts with none left, and [`race`](../features/concurrency/async-helpers.md) with `timeout=` when nothing wins in time |
| `RateLimitExceeded` | [`rate_limit`](../features/concurrency/rate-limits.md) and `RateLimiter` when `block=False` (the default) |
| `CircuitOpenError` | [`circuit_breaker`](../features/resilience/circuit-breaker.md) and `CircuitBreaker` while open (and while half-open with no probe slot left) |
| `BulkheadFull` | [`Bulkhead`](../features/concurrency/bulkheads.md) and `limit_concurrency` |
| `ValidationError` | [`validate_args`](../features/correctness/validation.md) |
| `ArgumentTypeError` | [`check_type`](../features/correctness/validation.md) and [`enforce_types`](../features/correctness/validation.md) |
| `BatchError` | [`batch_calls`](../features/caching/batching.md), when the batch function returns the wrong number of results, a mapping without this item's key, or something that is neither a sequence nor a mapping |

`retry` has no exception of its own. When attempts run out it re-raises the last error from your function, so callers handle the same exception types with or without retries.

## Catching them

Catch the specific class when you can act on it, and `PythonWrapError` to handle "python-wrap refused or cut off this call" in one place.

```python
from python_wrap import (
    CircuitBreaker, CircuitOpenError, PythonWrapError, RateLimitExceeded,
    rate_limit,
)

breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=30)

@breaker
def call_api() -> str:
    raise ConnectionError("down")

@rate_limit(1, 60)
def search(q: str) -> str:
    return q

try:
    call_api()
except ConnectionError:
    pass                                   # the first failure opens the circuit

try:
    call_api()
except CircuitOpenError as exc:
    assert 0 < exc.retry_after <= 30       # tell the caller when to come back

search("a")
try:
    search("b")
except RateLimitExceeded as exc:
    assert exc.retry_after > 0             # for a Retry-After header, for example

try:
    call_api()
except PythonWrapError as exc:             # one handler for every kind of refusal
    print(type(exc).__name__)
```

Because `TimeoutExpired` is a `TimeoutError`, and `ValidationError` a `ValueError`, built-in handlers still match:

```python
import time
from python_wrap import PythonWrapError, TimeoutExpired, timeout, validate_args

@timeout(0.01)
def slow() -> None:
    time.sleep(0.5)

try:
    slow()
except TimeoutError as exc:                # also matches TimeoutExpired
    assert isinstance(exc, TimeoutExpired) and exc.seconds == 0.01

@validate_args(n=lambda n: n > 0)
def sqrt_of(n: int) -> float:
    return n ** 0.5

try:
    sqrt_of(-1)
except ValueError as exc:                  # also matches ValidationError
    assert isinstance(exc, PythonWrapError)
    assert exc.parameter == "n" and exc.value == -1
```

!!! warning "Do not let `retry` swallow these"
    All of these are ordinary exceptions. A `retry` left at its default `on=Exception` retries `CircuitOpenError`, `TimeoutExpired` and `ValidationError` too, which rarely helps. Name the transient errors you want retried (`on=ConnectionError`). See [Stacking](stacking.md#rules-of-thumb).

!!! note "Catching `TimeoutError` also catches other timeouts"
    Built-in and library timeouts (`socket.timeout`, `asyncio.TimeoutError` on Python 3.11+) are `TimeoutError` as well. Catch `TimeoutExpired` when you mean only python-wrap's.

## See also

- [Choosing a decorator](choosing.md)
- [Stacking decorators](stacking.md)
- [API reference](../api/errors.md)
