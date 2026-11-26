# Circuit breaker

When a dependency is down, retrying makes things worse. A circuit breaker counts consecutive failures and, past a threshold, rejects calls immediately with `CircuitOpenError` for a while.

```python
import time
from python_wrap import CircuitOpenError, CircuitState, circuit_breaker

@circuit_breaker(failure_threshold=2, recovery_timeout=0.02, on=ConnectionError)
def call_api() -> str:
    raise ConnectionError("down")

for _ in range(2):
    try:
        call_api()
    except ConnectionError:
        pass

assert call_api.breaker.state is CircuitState.OPEN
try:
    call_api()
except CircuitOpenError as exc:
    assert exc.retry_after <= 0.02

time.sleep(0.03)
assert call_api.breaker.state is CircuitState.HALF_OPEN
call_api.breaker.reset()
assert call_api.breaker.state is CircuitState.CLOSED
```

## When to use it

- A downstream service that can go down for a while and should not be hammered, nor make every caller wait for a timeout.
- Failing fast so a caller can use a [fallback](fallback.md).
- Not for isolated glitches: that is [retry](retry.md). Use both: the breaker goes outside the retry and counts calls whose retries were exhausted.

## States

| State | Behaviour | Moves on when |
| --- | --- | --- |
| `closed` | Calls run normally. | `failure_threshold` consecutive failures: becomes `open`. |
| `open` | Calls are rejected with `CircuitOpenError`. | `recovery_timeout` seconds pass: becomes `half_open`. |
| `half_open` | A limited number of trial calls run. | A trial succeeds: `closed`. A trial fails: `open` again. |

Only exceptions matching `on=` count as failures; others pass through uncounted. `half_open_max_calls` limits how many trial calls may run at once while half-open.

## Sharing one breaker

`@circuit_breaker(...)` gives the function its own breaker, exposed as `wrapper.breaker`. To **share one breaker across several functions** (they all hit the same service), create a `CircuitBreaker` and use the instance as the decorator:

```python
from python_wrap import CircuitBreaker, CircuitOpenError

payments = CircuitBreaker(failure_threshold=1, recovery_timeout=60, name="payments")

@payments
def charge() -> None:
    raise RuntimeError("provider down")

@payments
def refund() -> None:
    ...

try:
    charge()
except RuntimeError:
    pass

try:
    refund()  # never runs: the shared circuit is open
except CircuitOpenError:
    pass
else:
    raise AssertionError("expected the circuit to be open")

assert payments.failure_count == 1
```

You can also call through a breaker without decorating: `breaker.call(func, *args, **kwargs)` (for an `async def` function it returns a coroutine to await).

## Tuning recovery, exclusions and monitoring

| Option | Default | What it does |
| --- | --- | --- |
| `success_threshold` | `1` | Consecutive successful trial calls needed to close a half-open circuit. Any counted failure during the trials re-opens it. |
| `exclude` | `()` | Exception types that are never counted, even when they match `on=` (for example a "not found" error that proves the service is up). They propagate unchanged. |
| `on_state_change` | `None` | `listener(breaker, old_state, new_state)`, called after every transition, outside the breaker's lock. An exception from the listener is logged, never raised. |
| `name` | function name | Used in log messages, `CircuitOpenError` messages and events. |

`breaker.stats` returns a frozen `CircuitStats` snapshot: `state`, total `failures`, `successes` and `rejected` calls (since creation or the last `reset()`), `consecutive_failures`, and `opened_at` (the `monotonic()` time the circuit last opened, `None` while closed).

```python
from python_wrap import CircuitBreaker, CircuitState

class NotFound(Exception):
    pass

changes = []
inventory = CircuitBreaker(
    failure_threshold=2,
    recovery_timeout=0,          # half-open again straight away, for the example
    success_threshold=2,         # two good trial calls before trusting it again
    exclude=NotFound,            # a 404 means the service is up
    on_state_change=lambda b, old, new: changes.append(new.value),
    name="inventory",
)

@inventory
def lookup(sku: str) -> int:
    if sku == "missing":
        raise NotFound(sku)
    if sku == "broken":
        raise ConnectionError("down")
    return 3

for sku in ("missing", "broken", "broken"):
    try:
        lookup(sku)
    except (NotFound, ConnectionError):
        pass

assert inventory.stats.failures == 2   # NotFound was not counted
assert lookup("ok") == 3               # first trial: still half-open
assert inventory.state is CircuitState.HALF_OPEN
assert lookup("ok") == 3               # second trial closes it
assert changes == ["open", "half_open", "closed"]
```

## Events

| Event | Fields |
| --- | --- |
| `circuit.state_changed` | `old`, `new` (`closed`, `open`, `half_open`) |
| `circuit.rejected` | `retry_after` (seconds) |

Both are reported under the breaker's name. See [Metrics](../observability/metrics.md).

## Gotchas

!!! note "One breaker per dependency"
    A breaker created by `@circuit_breaker(...)` belongs to that function alone. If several functions call the same service, share one `CircuitBreaker` instance, otherwise each learns separately that the service is down. A [policy](policies.md) with a `CircuitBreaker` also shares it across everything it decorates.

## Failure rate and slow calls

A count threshold does not scale with traffic. Five failures is an outage at 10 calls a minute and noise at 10,000. A rate does not change with the number of workers either. Slow calls can exhaust workers even when nothing fails, so the breaker can count those too.

```python
from python_wrap import CircuitBreaker

breaker = CircuitBreaker(
    name="search",
    window=60,
    failure_rate=0.5,
    minimum_calls=20,
    slow_call_duration=2.0,
    slow_call_rate=0.8,
)
assert breaker.stats.failure_rate == 0.0
```

- `failure_rate` is a share from 0 to 1, not 0. The breaker opens when at least `minimum_calls` (default `10`) calls were seen in the last `window` seconds and failures / calls >= `failure_rate`. It needs `window=`. Combining `failure_rate` with a non-default `failure_threshold` raises `ValueError`.
- `slow_call_duration` (seconds) and `slow_call_rate` (share) go together. A call that takes at least `slow_call_duration` counts as slow, whether it succeeds or fails. The breaker opens when slow calls / calls >= `slow_call_rate`, after `minimum_calls`. It needs `window=`. A slow half-open trial re-opens the breaker.
- Calls are counted in ten time slots per window, so old calls leave the window one slot at a time.
- `breaker.stats.failure_rate` and `breaker.stats.slow_rate` give the shares in the current window. They are `None` for a breaker without these options. The `circuit.state_changed` event gains `failure_rate` and `slow_rate` fields for such breakers.
- Shared state works the same way. `RedisBreakerState`, `AsyncRedisBreakerState` and `SQLiteBreakerState` keep per-slot counters for calls, failures and slow calls.

## See also

- [Retry](retry.md), [Fallback](fallback.md), [Policies](policies.md)
- [Errors](../../get-started/errors.md) for `CircuitOpenError`
- [API reference](../../api/circuit.md)
