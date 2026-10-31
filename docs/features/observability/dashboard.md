# Live dashboard

A single page that shows what the resilience decorators are doing right now: circuit breaker states, retry, give-up, rejection and timeout rates over the last minute, bulkhead saturation, per-function counters and the most recent events. It needs no assets from the internet and refreshes itself every 2 seconds from a JSON endpoint.

!!! warning "Not for public exposure"
    The dashboard has no authentication and shows function and breaker names and timing. Keep the default `127.0.0.1` binding, or mount it behind your own access control. Do not put it on the public internet.

```python
import json
import urllib.request

from python_wrap import _core
from python_wrap.dashboard import Dashboard

dashboard = Dashboard(recent=100)
server, thread = dashboard.serve()  # 127.0.0.1, free port
port = server.server_address[1]

_core.emit("cache.hit", "orders.load")
_core.emit("circuit.state_changed", "payments", old="closed", new="open")

with urllib.request.urlopen(f"http://127.0.0.1:{port}/snapshot.json") as response:
    data = json.load(response)
server.shutdown()

assert data["functions"]["orders.load"]["cache_hits"] == 1
assert data["breakers"]["payments"] == "open"
```

Open `http://127.0.0.1:<port>/` in a browser while your app runs. `serve()` starts a `wsgiref` server in a daemon thread and returns `(server, thread)`; `server.shutdown()` stops it, closes the socket and removes the event listener it installed.

## What it shows

* **Breakers and health**: states come from `circuit.state_changed` events and from the `HealthRegistry` report (`registry=`, default `python_wrap.health`), so breakers registered there appear even before they change state.
* **Bulkheads**: components in the registry with `active` and `waiting`.
* **Rates**: retries, gave-up calls, rejections and timeouts over the last 60 seconds.
* **Functions**: counters per function (capped at 500 names, the rest are grouped as `_other`).
* **Recent events**: a ring buffer of the last `recent` events (default 200). Only scalar fields are kept; exceptions are reduced to their type name, never the message.

Pass `exporter=` a `PrometheusExporter` to include its metric values under `metrics` in the snapshot.

## Mounting in an app

`Dashboard.wsgi_app` and `Dashboard.asgi_app` serve `/` (HTML) and `/snapshot.json`. Call `install()` once so it listens to events, and protect the route.

<!-- skip -->
```python
from fastapi import Depends, FastAPI
from python_wrap.dashboard import Dashboard

dashboard = Dashboard().install()
app = FastAPI(dependencies=[Depends(require_admin)])  # your own access check
app.mount("/resilience", dashboard.asgi_app)
```

`snapshot()` returns JSON-serialisable data and `render_html()` the page, if you prefer to build your own routes.
