# Exporters

Turn the [event stream](metrics.md) into Prometheus metrics or StatsD datagrams. Both exporters use only the standard library, subscribe with `install()`, and stop with `close()` (or a `with` block).

```python
from python_wrap import _core
from python_wrap.exporters import PrometheusExporter

with PrometheusExporter(registry=None) as exporter:
    _core.emit("retry.attempt_failed", "orders.fetch", attempt=1, exception=None, next_delay=0.5)
    _core.emit("cache.hit", "orders.load")
    text = exporter.render()

assert 'python_wrap_retry_attempts_total{function="orders.fetch"} 1' in text
assert 'python_wrap_cache_hits_total{function="orders.load"} 1' in text
```

In real code you never call `_core.emit` yourself: `retry`, `memoize`, `CircuitBreaker`, `Bulkhead` and the other decorators publish the events. Create the exporter once at start-up.

## Prometheus

`render()` returns the text exposition format 0.0.4, sorted so the output is stable. Label values are escaped (`\\`, `"` and newlines). Labels only ever contain event, function, breaker and component names and the fixed `kind`, never exception messages, arguments or cache keys.

### Serve it from your app

The exporter is a plain WSGI and ASGI app, so mount it wherever your framework allows, or add a route that returns `render()`.

<!-- skip -->
```python
# FastAPI / Starlette
from fastapi import FastAPI, Response
from python_wrap.exporters import CONTENT_TYPE, PrometheusExporter

exporter = PrometheusExporter().install()
app = FastAPI()

@app.get("/metrics")
def metrics() -> Response:
    return Response(exporter.render(), media_type=CONTENT_TYPE)

# or mount the ASGI app: app.mount("/metrics", exporter.asgi_app)
```

<!-- skip -->
```python
# Flask (mount the WSGI app next to the main one)
from flask import Flask
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from python_wrap.exporters import PrometheusExporter

exporter = PrometheusExporter().install()
app = Flask(__name__)
app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/metrics": exporter.wsgi_app})
```

<!-- skip -->
```python
# Django: a view in urls.py
from django.http import HttpResponse
from python_wrap.exporters import CONTENT_TYPE, PrometheusExporter

exporter = PrometheusExporter().install()

def metrics(request):
    return HttpResponse(exporter.render(), content_type=CONTENT_TYPE)
```

The apps answer `GET` and `HEAD` on `/metrics` (also `/` and an empty path, for mounted apps) with `Content-Type: text/plain; version=0.0.4; charset=utf-8`, `404` for other paths and `405` for other methods. They add no authentication; restrict the route like any other operational endpoint.

### Live gauges from the health registry

By default the exporter also reads `python_wrap.health` when you scrape, so breakers and bulkheads you registered appear as gauges without any event. Pass `registry=None` to turn that off, or your own `HealthRegistry`.

```python
from python_wrap.exporters import PrometheusExporter
from python_wrap.health import HealthRegistry

registry = HealthRegistry()
registry.register_check("database", lambda: True)

text = PrometheusExporter(registry=registry).render()
assert 'python_wrap_health_up{component="database"} 1' in text
```

### Bounded cardinality

`max_series` (default 10,000) caps the number of distinct label sets across all metrics. Once reached, new label sets are dropped (existing series keep updating) and `python_wrap_dropped_series_total` counts them. Alert on that counter if function names are generated dynamically.

```python
from python_wrap import _core
from python_wrap.exporters import PrometheusExporter

with PrometheusExporter(registry=None, max_series=2) as exporter:
    for i in range(10):
        _core.emit("cache.hit", f"fn{i}")

assert "python_wrap_dropped_series_total 18" in exporter.render()
```

### prometheus_client bridge

If your app already uses `prometheus_client`, register the exporter as a collector so everything appears on the existing registry and endpoint. The package is imported lazily and is not a dependency.

<!-- skip -->
```python
from python_wrap.exporters import PrometheusExporter

exporter = PrometheusExporter().install()
exporter.register_with_prometheus_client()  # default registry, or pass registry=...
```

### Metric reference

`python_wrap` below is the `namespace` argument.

| Metric | Type | Labels | Source |
| --- | --- | --- | --- |
| `python_wrap_events_total` | counter | `event`, `function` | every event |
| `python_wrap_retry_attempts_total` | counter | `function` | `retry.attempt_failed` |
| `python_wrap_retry_gave_up_total` | counter | `function` | `retry.gave_up` |
| `python_wrap_timeouts_total` | counter | `function` | `timeout.expired` |
| `python_wrap_rejections_total` | counter | `kind`, `function` | `circuit.rejected` (`circuit`), `rate_limit.rejected` (`rate_limit`), `bulkhead.rejected` (`bulkhead`) |
| `python_wrap_cache_hits_total` | counter | `function` | `cache.hit` |
| `python_wrap_cache_misses_total` | counter | `function` | `cache.miss` |
| `python_wrap_circuit_state` | gauge | `breaker` | `circuit.state_changed`: 0 closed, 1 half_open, 2 open |
| `python_wrap_retry_wait_seconds` | histogram | none | `next_delay` of `retry.attempt_failed` |
| `python_wrap_periodic_run_seconds` | histogram | `function` | `seconds` of `periodic.run` |
| `python_wrap_health_up` | gauge | `component` | health registry, read at scrape time |
| `python_wrap_bulkhead_active` | gauge | `component` | health registry (components with `active`/`waiting`) |
| `python_wrap_bulkhead_waiting` | gauge | `component` | health registry |
| `python_wrap_dropped_series_total` | counter | none | label sets dropped by `max_series` |

Histogram buckets default to `0.005` ... `60` seconds; pass `buckets=` to change them.

### Grafana

[Download the sample dashboard](../../assets/grafana-dashboard.json) and import it in Grafana. It has panels for breaker state, retry rate, gave-up rate, rejections by kind, timeouts, cache hit ratio, retry wait p95 and bulkhead saturation, using the `${DS_PROMETHEUS}` data source variable and a `function` filter. It assumes the default `python_wrap` namespace.

## StatsD

`StatsDExporter` sends UDP datagrams without ever blocking or raising. Send failures only increase its `errors` counter.

```python
import socket

from python_wrap import _core
from python_wrap.exporters import StatsDExporter

agent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
agent.bind(("127.0.0.1", 0))
agent.settimeout(2)

with StatsDExporter("127.0.0.1", agent.getsockname()[1], prefix="myapp") as statsd:
    _core.emit("cache.hit", "orders.load")
    assert agent.recv(1024) == b"myapp.cache.hit:1|c|#function:orders.load"

agent.close()
```

| Datagram | When |
| --- | --- |
| `<prefix>.<event>:1\|c\|#function:<name>` | every event |
| `<prefix>.retry.wait:<ms>\|ms` | `retry.attempt_failed` (`next_delay`) |
| `<prefix>.periodic.run:<ms>\|ms` | `periodic.run` |
| `<prefix>.rate_limit.wait:<ms>\|ms` | `rate_limit.waited` |
| `<prefix>.timing.slow:<ms>\|ms` | `timing.slow` |
| `<prefix>.circuit.state:<0\|1\|2>\|g\|#breaker:<name>` | `circuit.state_changed` |

Use `tags="none"` for plain StatsD without the `|#...` suffix, and `sample_rate=0.1` to send a tenth of the counters and timings (they carry `|@0.1`; gauges are never sampled). Tag values have `, | # : @` replaced by `_`.
