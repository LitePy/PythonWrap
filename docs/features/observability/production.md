# Context, telemetry, health, and slow calls

These helpers connect existing application logging and providers to decorator events.
They do not configure root logging, create OpenTelemetry providers, or expose HTTP
endpoints by themselves.

## Context on every application log

Attach `ContextFilter` to a **handler**, so propagated application records receive
context too. Explicit `extra=` attributes win; reserved LogRecord fields are not replaced.
Defaults make formatter fields available outside a request.

```python
import logging
from python_wrap import ContextFilter, bind, unbind

record = logging.LogRecord("app", logging.INFO, "app.py", 1, "hello", (), None)
record.user = "explicit"
token = bind(user="context", request_id="r1")
try:
    assert ContextFilter({"user": "-"}).filter(record)
    assert record.user == "explicit"
    assert record.request_id == "r1"
finally:
    unbind(token)
```

Use `bind(**fields)`/`unbind(token)` for framework before/after hooks. Tokens must be
reset in the same context that created them. `log_context(...)` remains the simpler
choice for a `with` block. Async task contexts are isolated. The
[framework adapters](../../cookbook/framework-integrations.md) manage request/task scope.

## OpenTelemetry events and metrics

Call `python_wrap.observability.enable(...)` explicitly. Without OpenTelemetry installed,
it returns None. With providers installed it returns a `Telemetry` handle; close it at
shutdown to remove the subscription. Python-wrap does not own provider shutdown.

<!-- skip -->
```python
from python_wrap.observability import enable

telemetry = enable(tracer_provider=my_tracer_provider,
                   meter_provider=my_meter_provider, max_functions=100)
# During application shutdown:
if telemetry is not None:
    telemetry.close()
```

Use a recent OpenTelemetry API with synchronous gauges for meter support. Event spans
are named `pw.<event-name>` and mark **notifications**, not the duration of an operation.
For an actual operation-duration span, keep `@trace(otel=True)` around that operation.

| Instrument | Source |
| --- | --- |
| `pw_retry_retries_total` | A failed attempt scheduled another retry. |
| `pw_cache_hits_total`, `pw_cache_misses_total` | Cache events. |
| `pw_bulkhead_rejected_total` | Rejected bulkhead calls. |
| `pw_breaker_rejected_total` | Open-circuit rejection events. |
| `pw_timeouts_total` | Expired timeout events. |
| `pw_limiter_wait_seconds` | Recorded limiter wait durations. |
| `pw_breaker_state` | Closed=0, half-open=1, open=2 on state-change events. |

Only the function/dependency name is a label. `max_functions` bounds observed names;
additional names become `_other`. Request ids, user ids, cache keys, arguments and
exception messages are never labels. With no subscription, existing event emission
keeps its cheap disabled path.

## Sentry breadcrumbs

<!-- skip -->
```python
import sentry_sdk
from python_wrap.observability import sentry_breadcrumbs

sentry_sdk.init(dsn="...")
sentry_breadcrumbs()  # retries, breaker changes, timeouts and rejections become breadcrumbs
```

Exception fields are reduced to type names; arguments, results and messages are never
sent. Pass `prefixes=` to choose which events are recorded, and call `.close()` on the
returned handle to stop.

## Slow-call detector

```python
from python_wrap import timed
from python_wrap.testing import fake_clock

@timed(warn_over="2s", log_args=True)
def generate_report(payload):
    clock.advance(3)
    return "done"

with fake_clock() as clock:
    assert generate_report({"password": "secret"}) == "done"
```

`timed` reports only calls at or above the threshold, including error/cancellation paths.
`log_args=True` masks sensitive parameter names and nested keys with `redact`;
`redact_names=` customizes those names. It does not log the return value. A `timing.slow`
event carries elapsed seconds. Use `timing` when you want every duration rather than a
slow-call threshold.

## Explicit health registry

```python
from python_wrap import CircuitBreaker, HealthRegistry

registry = HealthRegistry()
breaker = CircuitBreaker(name="catalog")
registry.register("catalog", breaker)
assert registry.report()["catalog"] == {"healthy": True, "state": "closed"}
breaker.force_open()
assert registry.report()["catalog"]["healthy"] is False
```

The global `health` object is also available. Registration is explicit and weak: a
registry does not keep a component alive. It reports breaker state, bulkhead counters,
and cache statistics without calling a registered application function. An unknown
component is marked registered but unchecked. Use `register_check(name, callable)` to
opt into backend probes. A probe failure reports the exception type, not its message.

HTTP health routes are opt-in through the framework adapters. Apply authentication and
choose what dependency details your application should disclose. Registry reports are
structural snapshots, not a guarantee that an external service will accept the next call.

See [logs API](../../api/logs.md), [telemetry API](../../api/observability.md),
[health API](../../api/health.md), and [slow-call API](../../api/slow.md).
