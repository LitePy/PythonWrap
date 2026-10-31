# Chaos and fault injection

Resilience code (retries, timeouts, circuit breakers, fallbacks) only earns its keep when something fails, and that is rare. `python_wrap.chaos` lets you cause the failures on purpose: add latency, raise errors or return canned values at marked call sites, in tests or during a staging game day.

It is a submodule import, like `python_wrap.testing`:

```python
from python_wrap.chaos import chaos, fault_point
```

## Mark the calls that may fail

`fault_point(name)` is a decorator for sync and async functions. It keeps the signature, and with no active rule it only checks that the rule list is empty.

```python
from python_wrap.chaos import chaos, fault_point

@fault_point("inventory.lookup")
def lookup(sku: str) -> int:
    return 42

assert lookup("a") == 42                       # no rule: behaves normally

with chaos.inject("inventory.lookup", result=0):
    assert lookup("a") == 0                    # real function not called
assert lookup("a") == 42                       # rule removed on exit
```

## Rules

`chaos.inject(name, ...)` returns a `Fault` handle. It is a context manager and also has `remove()`.

| Option | Meaning |
| --- | --- |
| `name` | Point name or fnmatch pattern such as `"payments.*"` |
| `latency` | Seconds, or a `(min, max)` range, waited before the call |
| `exception` | An instance, an exception class or a zero-argument factory; raised instead of calling |
| `result` | Returned instead of calling the function |
| `rate` | Probability (0 to 1) that a matching call is affected. Default 1.0 |
| `times` | Stop after this many injections |

Latency, errors and results can be combined across rules: latency rules apply first, and the first matching exception or result rule ends the call.

```python
from python_wrap.chaos import chaos, fault_point
from python_wrap.testing import fake_clock

@fault_point("payments.charge")
def charge() -> str:
    return "charged"

with fake_clock() as clock:
    with chaos.inject("payments.*", latency=0.5):
        assert charge() == "charged"
    assert clock.sleeps == [0.5]

with chaos.inject("payments.charge", exception=lambda: ConnectionError("reset")):
    try:
        charge()
    except ConnectionError as exc:
        assert str(exc) == "reset"
```

Latency waits through the same sleep as the rest of the package, so `fake_clock()` and `fast_mode()` control it and staging uses real time.

## Exercise your retries and timeouts

Because `fault_point` is a plain decorator, it can go in the `inner` slot of a `policy`. It then sits under the retry and timeout layers, which see every injected fault:

```python
from python_wrap import policy, retry
from python_wrap.chaos import chaos, fault_point
from python_wrap.testing import fast_mode

resilient = policy(
    retry=retry(max_attempts=3, delay=1, on=ConnectionError),
    inner=fault_point("db.query"),
)

@resilient
def query() -> str:
    return "rows"

with fast_mode():
    fault = chaos.inject("db.query", exception=ConnectionError, times=2)
    assert query() == "rows"        # two injected failures, third attempt succeeds
    assert fault.injected == 2
    fault.remove()
```

## Reproducible randomness

Rates and latency ranges use a random source owned by the injector. Seed it so a failing run can be replayed:

```python
from python_wrap.chaos import FaultInjector, fault_point

injector = FaultInjector(seed=42)

@fault_point("flaky", injector=injector)
def flaky() -> str:
    return "ok"

injector.inject("flaky", result="boom", rate=0.5)
first = [flaky() for _ in range(20)]

injector.reseed(42)
assert [flaky() for _ in range(20)] == first
assert injector.stats("flaky") == 2 * first.count("boom")
```

`chaos.reseed(n)` does the same for the global injector, and `FaultInjector(rng=random.Random(...))` accepts your own.

## Forcing a circuit open

```python
from python_wrap import CircuitBreaker
from python_wrap.chaos import chaos

breaker = CircuitBreaker(name="search")

with chaos.open_circuit(breaker):
    assert breaker.state.value == "open"     # test your fallback here
assert breaker.state.value == "closed"
```

A breaker that was already open stays open on exit.

## Game days in staging

`chaos.enable_from_env()` reads a JSON list of rules from `PW_CHAOS` (or another variable). Call it once at start-up:

```python
from python_wrap.chaos import chaos

chaos.enable_from_env()   # no-op unless PW_CHAOS is set
```

```bash
export PW_CHAOS='[
  {"name": "payments.*", "latency": 0.5, "rate": 0.1},
  {"name": "inventory", "exception": "ConnectionError", "times": 5},
  {"name": "search", "exception": "myapp.errors:UpstreamDown"}
]'
```

Each rule takes `name` plus any of `latency` (number or `[min, max]`), `exception` (a builtin name or `module:Class`), `result` (any JSON value), `rate` and `times`. Invalid JSON, unknown keys or an unknown exception raise `ValueError` with a message naming the variable and rule, and nothing is installed.

```python
from python_wrap.chaos import FaultInjector

inj = FaultInjector()
rules = inj.enable_from_env(environ={"PW_CHAOS": '[{"name": "a", "exception": "TimeoutError"}]'})
assert rules[0].exception is TimeoutError

try:
    inj.enable_from_env(environ={"PW_CHAOS": "[oops"})
except ValueError as exc:
    assert "PW_CHAOS is not valid JSON" in str(exc)
```

To stop all injection instantly, even with rules loaded, set the kill switch: `chaos.enabled = False`.

## Safety notes

- Nothing is injected unless a rule exists. With `PW_CHAOS` unset, `enable_from_env()` does nothing, so shipping the call is safe in production.
- Do not set `PW_CHAOS` in production deployments; keep it in staging configuration only.
- Prefer `rate` and `times` in shared environments so a game day degrades traffic instead of stopping it.
- Rules are per process. In a multi-worker server each worker loads the variable on its own, with its own random source and `times` budget.

## Observing injections

Each injection emits a `chaos.injected` event with `kind` (`"latency"`, `"exception"` or `"result"`), `point` and `pattern`. `chaos.stats("name")` returns the count at a point and `chaos.stats()` all of them.

```python
from python_wrap.chaos import chaos, fault_point
from python_wrap.events import listening

@fault_point("demo")
def demo() -> int:
    return 1

seen = []
with listening(seen.append), chaos.inject("demo", result=2):
    demo()
assert [(e.name, e.fields["kind"]) for e in seen] == [("chaos.injected", "result")]
chaos.clear()
```
