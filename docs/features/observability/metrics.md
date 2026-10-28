# Metrics and events

See what your code does in production, with no dependencies: in-process call statistics, and an event stream you can forward to your monitoring system.

```python
from python_wrap.metrics import count_calls

@count_calls(window=500)
def work(n: int) -> int:
    if n < 0:
        raise ValueError("negative")
    return n * 2

work(1)
work(2)
try:
    work(-1)
except ValueError:
    pass

stats = work.stats
assert (stats.calls, stats.errors, stats.in_flight) == (3, 1, 0)
assert stats.last_duration >= 0
assert 0 <= stats.p50 <= stats.p95 <= stats.p99
work.reset()
assert work.stats.calls == 0
```

## When to use it

- `count_calls` when you want call counts, error rate and p50/p95/p99 for one function, read from your own code or an endpoint.
- Events when you want to know what the resilience decorators did (retries, cache hits, circuit changes, rejections) and forward it to Prometheus, OpenTelemetry, statsd or your logs.
- For per-call durations written to the log, use [`timing`](timing.md). For arguments and results, use [Logging](logging.md).

## `count_calls`

`.stats` is a live view with `calls`, `errors`, `in_flight`, `last_duration`, `mean`, `p50`, `p95` and `p99` (seconds). `calls` and `errors` count everything since the last `reset()`; the latency figures cover the last `window` finished calls (default 1000), failed ones included. Percentiles use the nearest-rank method. All of it is thread-safe.

`async def` functions work the same, timed across the whole awaited coroutine, and `in_flight` shows how many calls are running right now:

```python
import asyncio
from python_wrap.metrics import count_calls

@count_calls
async def fetch(n: int) -> int:
    await asyncio.sleep(0)
    return n

async def main() -> None:
    await asyncio.gather(*(fetch(i) for i in range(5)))
    assert fetch.stats.calls == 5

asyncio.run(main())
```

!!! note "Methods share statistics"
    On methods the statistics belong to the function, so they are shared by every instance. Cancellation counts as an error.

## Events

Decorators publish `Event(name, function, fields)` objects. Subscribe to receive them:

```python
from python_wrap.retry import retry
from python_wrap.events import Event, listening

seen: list[Event] = []
calls = 0

@retry(max_attempts=3, delay=0)
def flaky() -> str:
    global calls
    calls += 1
    if calls < 3:
        raise ConnectionError("down")
    return "ok"

with listening(seen.append):
    assert flaky() == "ok"

names = [e.name for e in seen]
assert names == ["retry.attempt_failed", "retry.attempt_failed", "retry.succeeded"]
assert seen[0].fields["attempt"] == 1
```

- `subscribe(listener)` registers a listener and returns a function that unsubscribes it.
- `listening(listener)` is the same as a `with` block.
- `filter_events("retry.", listener)` wraps a listener so it only sees names with that prefix (or any of a tuple of prefixes).

```python
from python_wrap.events import Event, filter_events, subscribe

retries: list[Event] = []
unsubscribe = subscribe(filter_events("retry.", retries.append))
unsubscribe()
```

!!! warning "Listeners run in the calling thread"
    Listeners run synchronously in the thread that triggered the event, so keep them fast and never block. An exception in a listener is logged and swallowed: monitoring code can not break the decorated function. With no listeners registered, emitting is a single list check.

### Event catalogue

`python_wrap.events.EVENT_NAMES` maps each name to a description; this table is the same data. `Event.function` is the decorated function's qualified name (for `circuit.*` events it is the breaker's name).

| Event | Fields |
| --- | --- |
| `retry.attempt_failed` | `attempt` (int, 1-based), `exception` (`None` for a rejected result), `next_delay` (seconds) |
| `retry.succeeded` | `attempts` (total made) |
| `retry.gave_up` | `attempts`, `exception` (`None` for a rejected result) |
| `retry.budget_exhausted` | `attempts`, `exception`: a shared `RetryBudget` refused the retry (`retry.gave_up` follows) |
| `timeout.expired` | `seconds` (the limit) |
| `circuit.state_changed` | `old`, `new` (`closed`, `open`, `half_open`) |
| `circuit.rejected` | `retry_after` (seconds) |
| `rate_limit.rejected` | `retry_after` (seconds) |
| `rate_limit.waited` | `seconds` |
| `throttle.skipped` | none |
| `cache.hit` | none |
| `cache.miss` | none |
| `cache.evicted` | `reason` (`lru` or `ttl`) |
| `bulkhead.rejected` | `active`, `waiting` |

```python
from python_wrap.events import EVENT_NAMES

assert "cache.hit" in EVENT_NAMES
for name, description in EVENT_NAMES.items():
    assert description
```

## Recipes

None of these libraries are dependencies of python-wrap, so the blocks below are not run by the docs checker. Each is a few lines: subscribe once at start-up.

### Prometheus

<!-- skip -->
```python
from prometheus_client import Counter
from python_wrap.events import Event, subscribe

_counters: dict[str, Counter] = {}

def to_prometheus(event: Event) -> None:
    name = "python_wrap_" + event.name.replace(".", "_") + "_total"
    counter = _counters.get(name)
    if counter is None:
        counter = _counters[name] = Counter(name, event.name, ["function"])
    counter.labels(function=event.function).inc()

subscribe(to_prometheus)
```

Use a fixed set of names (for instance built from `EVENT_NAMES`) to avoid the lookup race on first use, and keep `function` as the only label: never label with exception messages.

### OpenTelemetry

<!-- skip -->
```python
from opentelemetry import metrics, trace
from python_wrap.events import Event, subscribe

counter = metrics.get_meter("python_wrap").create_counter("python_wrap.events")

def to_otel(event: Event) -> None:
    counter.add(1, {"event": event.name, "function": event.function})
    span = trace.get_current_span()          # annotate the active span, if any
    if span.is_recording():
        span.add_event(event.name, {"function": event.function})

subscribe(to_otel)
```

### statsd

<!-- skip -->
```python
from statsd import StatsClient
from python_wrap.events import Event, subscribe

statsd = StatsClient()

def to_statsd(event: Event) -> None:
    statsd.incr(f"python_wrap.{event.name}")

subscribe(to_statsd)
```

### structlog or logging

<!-- skip -->
```python
import structlog
from python_wrap.events import Event, subscribe

log = structlog.get_logger("python_wrap.events")

def to_structlog(event: Event) -> None:
    fields = {k: v for k, v in event.fields.items() if k != "exception"}
    log.info(event.name, function=event.function, **fields)

subscribe(to_structlog)
```

With the standard library instead, call `logging.getLogger("myapp.events").info("%s %s", event.name, event.function, extra={"fields": event.fields})`.

## See also

- [Timing](timing.md), [Logging](logging.md)
- [Retry](../resilience/retry.md), [Circuit breaker](../resilience/circuit-breaker.md) and the other pages that list the events they emit
- [API reference](../../api/metrics.md), [events API](../../api/events.md)
