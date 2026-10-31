# Migrating from pybreaker

python-wrap's `CircuitBreaker` has the same closed, open and half-open cycle as pybreaker. The constructor uses different names, listeners become a single callback, and shared state across processes is a `backend=` object instead of a storage class. There is no codemod for this one: the translation is a handful of keyword renames, mostly done by hand.

## At a glance

=== "pybreaker"

    <!-- skip -->
    ```python
    import pybreaker

    breaker = pybreaker.CircuitBreaker(fail_max=5, reset_timeout=30, exclude=[KeyError], name="payments")

    @breaker
    def charge(order_id: int) -> str: ...

    try:
        charge(1)
    except pybreaker.CircuitBreakerError:
        ...
    ```

=== "python-wrap"

    ```python
    from python_wrap import CircuitBreaker, CircuitOpenError

    breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30, exclude=KeyError, name="payments")

    @breaker
    def charge(order_id: int) -> str:
        return f"charged {order_id}"

    try:
        assert charge(1) == "charged 1"
    except CircuitOpenError as exc:
        print(f"open, retry in {exc.retry_after:.0f}s")
    ```

## Constructor

| pybreaker | python-wrap | Notes |
| --- | --- | --- |
| `fail_max=5` | `failure_threshold=5` | consecutive failures that open the circuit |
| `reset_timeout=60` | `recovery_timeout=30.0` | default differs: 60 in pybreaker, 30 here |
| `success_threshold=1` | `success_threshold=1` | consecutive successful trials needed to close |
| `exclude=[A, B]` (types or predicates) | `exclude=(A, B)` (types) | a list becomes a type or tuple; predicates have no equivalent, see below |
| `listeners=[...]` | `on_state_change=` | one callback `(breaker, old, new)` |
| `state_storage=CircuitMemoryStorage(...)` | the default | in-process state |
| `state_storage=CircuitRedisStorage(...)` | `backend=RedisBreakerState(client)` | needs `name=` |
| `name="x"` | `name="x"` | required with `backend=` |
| `throw_new_error_on_trip=True` | not needed | the tripping call always re-raises the original exception |
| (not available) | `on=Exception` | restrict which exceptions count as failures |
| (not available) | `half_open_max_calls=1` | concurrent trial calls while half-open |
| (not available) | `window=seconds` | count failures inside a rolling window instead of consecutive failures |

Exclusions by predicate (`exclude=[lambda e: getattr(e, "status", 0) < 500]`) can be expressed by narrowing `on=` to the exception types you want counted, or by catching the exception inside the protected function and returning normally.

## Calls and state

| pybreaker | python-wrap |
| --- | --- |
| `@breaker` | `@breaker` (sync and `async def`); or `@circuit_breaker(...)` for a private breaker, exposed as `wrapper.breaker` |
| `breaker.call(f, *a, **kw)` | `breaker.call(f, *a, **kw)` (returns a coroutine for `async def`) |
| `breaker.call_async(f, ...)` | `await breaker.call(f, ...)` |
| `with breaker.calling():` | decorate a small function and call it |
| `breaker.current_state` (`"closed"`, `"open"`, `"half-open"`) | `breaker.state` (`CircuitState.CLOSED`, `OPEN`, `HALF_OPEN`; values are `"closed"`, `"open"`, `"half_open"`) |
| `breaker.fail_counter` | `breaker.failure_count`; totals are in `breaker.stats` |
| `breaker.success_counter` | `breaker.stats.successes` |
| `breaker.open()` | `breaker.force_open()` |
| `breaker.close()` | `breaker.reset()` (also zeroes the counters) |
| `breaker.half_open()` | not available; wait for `recovery_timeout` |
| `pybreaker.CircuitBreakerError` | `CircuitOpenError`, with `retry_after` in seconds |
| `breaker.add_listener(l)` / `remove_listener` | pass `on_state_change=` when constructing |

```python
from python_wrap import CircuitBreaker, CircuitOpenError, CircuitState

breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60, name="inventory")

@breaker
def lookup(sku: str) -> str:
    raise ConnectionError("down")

for _ in range(2):
    try:
        lookup("a")
    except ConnectionError:
        pass

assert breaker.state is CircuitState.OPEN
try:
    lookup("a")
except CircuitOpenError as exc:
    assert exc.retry_after > 0

breaker.reset()
assert breaker.state is CircuitState.CLOSED
print(breaker.stats)
```

## Listeners

pybreaker listeners are objects with `before_call`, `call`, `failure`, `success` and `state_change` methods. python-wrap has one callback for state changes, plus [events](../features/observability/index.md) for the rest (`circuit.state_changed`, `circuit.rejected`).

=== "pybreaker"

    <!-- skip -->
    ```python
    class LogListener(pybreaker.CircuitBreakerListener):
        def state_change(self, cb, old_state, new_state):
            log.warning("%s: %s -> %s", cb.name, old_state.name, new_state.name)

    breaker = pybreaker.CircuitBreaker(listeners=[LogListener()])
    ```

=== "python-wrap"

    ```python
    from python_wrap import CircuitBreaker, CircuitState

    changes: list[tuple[str, str]] = []

    def on_change(breaker: CircuitBreaker, old: CircuitState, new: CircuitState) -> None:
        changes.append((old.value, new.value))

    breaker = CircuitBreaker(failure_threshold=1, on_state_change=on_change, name="cb")

    @breaker
    def boom() -> None:
        raise OSError

    try:
        boom()
    except OSError:
        pass
    assert changes == [("closed", "open")]
    ```

The callback runs outside the breaker's lock, and an exception raised by it is logged, never propagated into the protected call. State changes and rejected calls are also published as events:

```python
from python_wrap import CircuitBreaker, events

seen: list[str] = []
breaker = CircuitBreaker(failure_threshold=1, name="events-demo")

@breaker
def boom() -> None:
    raise OSError

with events.listening(lambda event: seen.append(event.name)):
    try:
        boom()
    except OSError:
        pass
    try:
        boom()
    except Exception:
        pass

assert seen == ["circuit.state_changed", "circuit.rejected"]
```

There is no per-call `before_call`, `failure` or `success` hook; wrap the function yourself if you need one. See the [observability](../features/observability/index.md) pages for the full list of event names.

## Shared state across processes

=== "pybreaker"

    <!-- skip -->
    ```python
    import pybreaker, redis

    client = redis.StrictRedis()
    breaker = pybreaker.CircuitBreaker(
        fail_max=5,
        reset_timeout=30,
        state_storage=pybreaker.CircuitRedisStorage(pybreaker.STATE_CLOSED, client, namespace="payments"),
        name="payments",
    )
    ```

=== "python-wrap"

    <!-- skip -->
    ```python
    import redis
    from python_wrap import CircuitBreaker, RedisBreakerState

    client = redis.Redis()
    breaker = CircuitBreaker(
        failure_threshold=5,
        recovery_timeout=30,
        backend=RedisBreakerState(client),
        name="payments",     # required: it keys the shared state
    )
    ```

Use the same `name` and settings in every process. `RedisBreakerState` runs each decision as one atomic Lua script, bounds half-open trial leases (so a crashed prober cannot hold the only trial slot forever) and propagates Redis errors instead of treating them as a healthy dependency. Unlike `CircuitRedisStorage`'s `fallback_circuit_state`, there is no silent fallback to "closed" when Redis is down: decide what your service should do when the backend raises. python-wrap never imports `redis` itself; you pass the client.

## Behavioural differences to check

1. **Which exception callers see.** pybreaker raises `CircuitBreakerError` from the call that trips the breaker (unless `throw_new_error_on_trip=False`). python-wrap re-raises the original exception on that call and raises `CircuitOpenError` only for calls made while open. Catch `CircuitOpenError` where you caught `CircuitBreakerError`.
2. **Excluded exceptions are neutral.** They count neither as a failure nor as a success, so they do not reset the failure count. pybreaker's exclusion counts them as successes.
3. **A success resets the count.** Failures are consecutive by default. Use `window=` to open on N failures inside a rolling period instead.
4. **Half-open.** One trial call at a time by default (`half_open_max_calls=1`); other callers get `CircuitOpenError` with `retry_after=0.0` while a trial is in flight. `success_threshold` consecutive successful trials close the circuit; any counted failure reopens it.
5. **`reset_timeout` default.** 60 seconds in pybreaker, 30 in python-wrap's `recovery_timeout`. Set it explicitly.
6. **Reading `state` after the timeout.** An open breaker past its timeout reports `half_open`.
7. **Tests.** `python_wrap.testing.fake_clock()` advances `recovery_timeout` without sleeping.
8. **Pickling.** Breakers refuse to pickle (they hold locks and, optionally, a client); create them per process.
