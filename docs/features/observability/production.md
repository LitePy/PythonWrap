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

Options that control what is recorded:

- `spans="events"` (default) adds each event as a span event named `pw.<event>` on the
  current span.
- `spans="notifications"` opens and closes a zero-length `pw.<event>` span per event, as
  earlier betas did.
- `spans="off"` adds nothing.
- `attempt_spans=True` opens one `python_wrap.retry.attempt` span per retry attempt, with
  its real duration. Attributes are `python_wrap.retry.attempt` (the attempt number) and
  `python_wrap.retry.outcome` (`retry`, `gave_up` or `success`).
- `wait_spans=True` opens one `python_wrap.rate_limit.wait` span per rate-limiter wait.
- `naming="legacy"` (default) keeps the `pw_*` metric names. `naming="otel"` uses
  OpenTelemetry-style names with units.

Attempt and wait spans are opt-in to control volume. Retry events also carry
`attempt_seconds`.

Breaking change in this beta: the default span behaviour changed from notification spans
to span events. Use `spans="notifications"` to get the old spans.

<!-- skip -->
```python
from python_wrap.observability import enable

telemetry = enable(naming="otel", attempt_spans=True, wait_spans=True)
```

For an actual operation-duration span, keep `@trace(otel=True)` around that operation.

| Event or state | Type | Legacy name | OpenTelemetry name |
| --- | --- | --- | --- |
| Retry scheduled | counter | `pw_retry_retries_total` | `python_wrap.retry.retries` |
| Gave up | counter | `pw_retry_gave_up_total` | `python_wrap.retry.gave_up` |
| Retry budget exhausted | counter | `pw_retry_budget_exhausted_total` | `python_wrap.retry.budget_exhausted` |
| Cache hit | counter | `pw_cache_hits_total` | `python_wrap.cache.hits` |
| Cache miss | counter | `pw_cache_misses_total` | `python_wrap.cache.misses` |
| Bulkhead rejected | counter | `pw_bulkhead_rejected_total` | `python_wrap.bulkhead.rejected` |
| Breaker rejected | counter | `pw_breaker_rejected_total` | `python_wrap.circuit.rejected` |
| Rate limit rejected | counter | `pw_rate_limit_rejected_total` | `python_wrap.rate_limit.rejected` |
| Timeout | counter | `pw_timeouts_total` | `python_wrap.timeouts` |
| Backend error | counter | `pw_backend_errors_total` | `python_wrap.backend.errors` |
| Token-window store error | counter | `pw_rate_limit_store_errors_total` | `python_wrap.rate_limit.store_errors` |
| Limiter wait (seconds) | histogram | `pw_limiter_wait_seconds` | `python_wrap.rate_limit.wait` |
| Breaker state (0 closed, 1 half-open, 2 open) | observable gauge | `pw_breaker_state` | `python_wrap.circuit.state` |
| Component health | observable gauge | `pw_health_up` | `python_wrap.health.up` |

The gauges are read at each collection, so a meter provider must be set for them. Breaker
state covers every breaker in the process. With a shared breaker, each process reports the
shared state (within the backend's `state_ttl`), not only the process that saw the change.
Health gauges carry a `component` attribute from the health registry. Pass
`registry=False` to leave it out.

Only the function/dependency name is a label. `max_functions` bounds observed names;
additional names become `_other`.
Service attributes such as `service.name` come from the provider's resource. Request ids,
user ids, cache keys, arguments and exception messages are never labels. With no
subscription, existing event emission keeps its cheap disabled path.

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

### Overall status and health endpoints

`summary(critical=...)` rolls the report up into one status a load balancer or dashboard
can use: `ok`, `degraded` (only optional components unhealthy, still HTTP 200) or `down`
(a critical component unhealthy, HTTP 503). `wsgi_app()` and `asgi_app()` serve that
summary as JSON:

```python
from python_wrap import CircuitBreaker, HealthRegistry

registry = HealthRegistry()
registry.register_check("database", lambda: True)
search = CircuitBreaker(name="search")
registry.register("search", search)
search.force_open()

summary = registry.summary(critical=["database"])
assert summary["status"] == "degraded" and summary["http_status"] == 200
assert summary["unhealthy"] == ["search"]
```

<!-- skip -->
```python
from fastapi import FastAPI
from python_wrap import health

app = FastAPI()
app.mount("/health", health.asgi_app(critical=["database"]))
```

Pair it with the [Prometheus exporter](exporters.md), which publishes the same
components as `python_wrap_health_up` gauges, and the sample Grafana dashboard in
`docs/assets/grafana-dashboard.json`.

HTTP health routes are opt-in through the framework adapters. Apply authentication and
choose what dependency details your application should disclose. Registry reports are
structural snapshots, not a guarantee that an external service will accept the next call.

See [logs API](../../api/logs.md), [telemetry API](../../api/observability.md),
[health API](../../api/health.md), and [slow-call API](../../api/slow.md).
