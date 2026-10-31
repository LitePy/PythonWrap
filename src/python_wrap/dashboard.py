"""A small live dashboard of resilience state for local development and staging.

`Dashboard` listens to the event stream, keeps a bounded ring buffer of recent events and
per-function counters, combines them with a `HealthRegistry` report, and serves a single
self-contained HTML page that refreshes itself from a JSON endpoint.

The dashboard has no authentication. Bind it to localhost, or mount it behind your own
access control. Never expose it publicly: function names, breaker names and event timing
are operational details. Exception messages are never recorded, only exception type names.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Mapping
from socketserver import ThreadingMixIn
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from python_wrap import _core, _fork
from python_wrap.events import Event, subscribe
from python_wrap.exporters import PrometheusExporter
from python_wrap.health import HealthRegistry, health

__all__ = ["Dashboard"]

_RATE_WINDOW = 60.0
_MAX_FUNCTIONS = 500
_COUNTER_KEYS = {
    "retry.attempt_failed": "retries",
    "retry.gave_up": "gave_up",
    "timeout.expired": "timeouts",
    "circuit.rejected": "rejections",
    "rate_limit.rejected": "rejections",
    "bulkhead.rejected": "rejections",
    "cache.hit": "cache_hits",
    "cache.miss": "cache_misses",
}
_RATE_KINDS = {
    "retry.attempt_failed": "retries",
    "retry.gave_up": "gave_up",
    "circuit.rejected": "rejections",
    "rate_limit.rejected": "rejections",
    "bulkhead.rejected": "rejections",
    "timeout.expired": "timeouts",
}


def _safe_field(value: Any) -> Any:
    """Reduce a field to something JSON-safe and free of messages."""
    if isinstance(value, BaseException):
        return type(value).__name__
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:100]
    return type(value).__name__


class Dashboard:
    """Collect recent events and health state and present them as JSON or HTML.

    Args:
        registry: `HealthRegistry` whose report is shown (breakers, bulkheads, checks).
        exporter: Optional `PrometheusExporter`; its metrics are included in the snapshot.
        recent: How many recent events to keep.

    Raises:
        ValueError: If ``recent`` is below 1.

    Example:
        >>> from python_wrap import _core
        >>> with Dashboard(recent=10) as dash:
        ...     _core.emit("cache.hit", "load")
        ...     dash.snapshot()["functions"].get("load", {}).get("cache_hits")
        1
    """

    def __init__(
        self,
        *,
        registry: HealthRegistry | None = health,
        exporter: PrometheusExporter | None = None,
        recent: int = 200,
    ) -> None:
        if recent < 1:
            raise ValueError("recent must be at least 1")
        self.registry = registry
        self.exporter = exporter
        self._lock = _fork.ForkSafeLock()
        self._events: deque[dict[str, Any]] = deque(maxlen=recent)
        self._functions: dict[str, dict[str, int]] = {}
        self._breakers: dict[str, str] = {}
        self._recent_rates: deque[tuple[float, str]] = deque(maxlen=10_000)
        self._started = _core.wall_time()
        self._unsubscribe: Callable[[], None] | None = None

    # ------------------------------------------------------------------ lifecycle

    def install(self) -> Dashboard:
        """Subscribe to the event stream. Returns this dashboard."""
        with self._lock:
            if self._unsubscribe is None:
                self._unsubscribe = subscribe(self._on_event)
        return self

    def close(self) -> None:
        """Unsubscribe. Collected data stays available."""
        with self._lock:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    def __enter__(self) -> Dashboard:
        return self.install()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ collection

    def _on_event(self, event: Event) -> None:
        fields = {k: _safe_field(v) for k, v in event.fields.items()}
        record = {
            "time": _core.wall_time(),
            "event": event.name,
            "function": event.function,
            "fields": fields,
        }
        now = _core.monotonic()
        with self._lock:
            self._events.append(record)
            name = event.function
            if name not in self._functions and len(self._functions) >= _MAX_FUNCTIONS:
                name = "_other"
            counters = self._functions.setdefault(
                name,
                {
                    "events": 0,
                    "retries": 0,
                    "gave_up": 0,
                    "timeouts": 0,
                    "rejections": 0,
                    "cache_hits": 0,
                    "cache_misses": 0,
                },
            )
            counters["events"] += 1
            key = _COUNTER_KEYS.get(event.name)
            if key is not None:
                counters[key] += 1
            kind = _RATE_KINDS.get(event.name)
            if kind is not None:
                self._recent_rates.append((now, kind))
            if event.name == "circuit.state_changed":
                self._breakers[event.function] = str(fields.get("new"))

    # ------------------------------------------------------------------ output

    def snapshot(self) -> dict[str, Any]:
        """Return the current state as JSON-serialisable data.

        Keys: ``time``, ``uptime_seconds``, ``health`` (registry report), ``breakers``
        (name to last observed state), ``functions`` (per-function counters), ``rates``
        (counts over the last 60 seconds), ``events`` (newest last) and, when an exporter
        was given, ``metrics``.
        """
        report = self.registry.report() if self.registry is not None else {}
        now = _core.monotonic()
        with self._lock:
            events = list(self._events)
            functions = {name: dict(c) for name, c in self._functions.items()}
            breakers = dict(self._breakers)
            rates = {"window_seconds": _RATE_WINDOW, **dict.fromkeys(set(_RATE_KINDS.values()), 0)}
            for stamp, kind in self._recent_rates:
                if now - stamp <= _RATE_WINDOW:
                    rates[kind] += 1
        for name, info in report.items():
            state = info.get("state")
            if isinstance(state, str):
                breakers[name] = state
        data: dict[str, Any] = {
            "time": _core.wall_time(),
            "uptime_seconds": round(_core.wall_time() - self._started, 3),
            "health": report,
            "breakers": breakers,
            "functions": functions,
            "rates": rates,
            "events": events,
        }
        if self.exporter is not None:
            data["metrics"] = self.exporter.snapshot()
        return json.loads(json.dumps(data, default=_safe_field))  # type: ignore[no-any-return]

    def render_html(self) -> str:
        """Return the dashboard as one self-contained HTML page (no external assets)."""
        return _HTML

    # ------------------------------------------------------------------ serving

    def _respond(self, method: str, path: str) -> tuple[str, str, bytes]:
        if method not in ("GET", "HEAD"):
            return "405 Method Not Allowed", "text/plain; charset=utf-8", b"method not allowed\n"
        if path.endswith("/snapshot.json"):
            body = json.dumps(self.snapshot()).encode("utf-8")
            return "200 OK", "application/json", body
        if path in ("", "/") or path.endswith("/"):
            return "200 OK", "text/html; charset=utf-8", self.render_html().encode("utf-8")
        return "404 Not Found", "text/plain; charset=utf-8", b"not found\n"

    def wsgi_app(
        self, environ: Mapping[str, Any], start_response: Callable[..., Any]
    ) -> list[bytes]:
        """WSGI application serving ``/`` (HTML) and ``/snapshot.json``."""
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        status, ctype, body = self._respond(method, str(environ.get("PATH_INFO", "")))
        start_response(
            status,
            [
                ("Content-Type", ctype),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [] if method == "HEAD" else [body]

    async def asgi_app(
        self, scope: Mapping[str, Any], receive: Callable[..., Any], send: Callable[..., Any]
    ) -> None:
        """ASGI application serving ``/`` (HTML) and ``/snapshot.json``."""
        if scope.get("type") == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                else:
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope.get("type") != "http":
            return
        method = str(scope.get("method", "GET")).upper()
        status, ctype, body = self._respond(method, str(scope.get("path", "")))
        headers = [
            (b"content-type", ctype.encode("ascii")),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
        await send({"type": "http.response.start", "status": int(status[:3]), "headers": headers})
        await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})

    def serve(self, host: str = "127.0.0.1", port: int = 0) -> tuple[WSGIServer, threading.Thread]:
        """Serve the dashboard from a daemon thread, for local development.

        Installs the event listener if needed. Call ``server.shutdown()`` to stop it; that
        also closes the socket and removes the listener this call installed. There is no
        authentication: keep the default localhost binding.

        Args:
            host: Interface to bind. Defaults to localhost only.
            port: Port to bind; ``0`` picks a free one (see ``server.server_address``).

        Returns:
            The server and its running thread.
        """
        was_installed = self._unsubscribe is not None
        self.install()
        dashboard = self

        class _Server(ThreadingMixIn, WSGIServer):
            daemon_threads = True

            def shutdown(self) -> None:
                super().shutdown()
                self.server_close()
                if not was_installed:
                    dashboard.close()

        class _Quiet(WSGIRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

        server = make_server(host, port, self.wsgi_app, server_class=_Server, handler_class=_Quiet)
        thread = threading.Thread(
            target=server.serve_forever, name="python-wrap-dashboard", daemon=True
        )
        thread.start()
        return server, thread


_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>python-wrap dashboard</title>
<style>
:root { color-scheme: light dark; --bg:#fff; --fg:#1b1f23; --mut:#6a737d; --line:#d0d7de;
  --ok:#1a7f37; --warn:#9a6700; --bad:#cf222e; }
@media (prefers-color-scheme: dark) { :root { --bg:#0d1117; --fg:#e6edf3; --mut:#8b949e;
  --line:#30363d; --ok:#3fb950; --warn:#d29922; --bad:#f85149; } }
body { margin:0; padding:16px; font:14px/1.4 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
h1 { font-size:18px; margin:0 0 4px; } h2 { font-size:14px; margin:20px 0 6px; }
.mut { color:var(--mut); } table { border-collapse:collapse; width:100%; }
th, td { text-align:left; padding:4px 8px; border-bottom:1px solid var(--line); font-variant-numeric:tabular-nums; }
.pill { padding:1px 8px; border-radius:10px; border:1px solid currentColor; }
.closed, .up { color:var(--ok); } .half_open { color:var(--warn); } .open, .down { color:var(--bad); }
.bar { display:inline-block; height:8px; background:var(--warn); vertical-align:middle; }
.cards { display:flex; flex-wrap:wrap; gap:12px; } .card { border:1px solid var(--line); border-radius:6px; padding:8px 12px; min-width:110px; }
.card b { display:block; font-size:20px; }
</style>
</head>
<body>
<h1>python-wrap dashboard</h1>
<div class="mut" id="status">loading...</div>
<h2>Last 60 seconds</h2><div class="cards" id="rates"></div>
<h2>Circuit breakers</h2><table id="breakers"></table>
<h2>Bulkheads</h2><table id="bulkheads"></table>
<h2>Health</h2><table id="health"></table>
<h2>Functions</h2><table id="functions"></table>
<h2>Recent events</h2><table id="events"></table>
<script>
var ENDPOINT = "snapshot.json";
function el(tag, text, cls) { var e = document.createElement(tag); if (text !== undefined) e.textContent = String(text); if (cls) e.className = cls; return e; }
function fill(id, head, rows) {
  var t = document.getElementById(id); t.textContent = "";
  var tr = el("tr"); head.forEach(function (h) { tr.appendChild(el("th", h)); }); t.appendChild(tr);
  if (!rows.length) { var e = el("tr"); var c = el("td", "none", "mut"); c.colSpan = head.length; e.appendChild(c); t.appendChild(e); }
  rows.forEach(function (r) { var row = el("tr"); r.forEach(function (c) { var td = el("td");
    if (c && c.node) td.appendChild(c.node); else td.textContent = String(c); row.appendChild(td); }); t.appendChild(row); });
}
function render(s) {
  document.getElementById("status").textContent = "updated " + new Date(s.time * 1000).toLocaleTimeString() + ", up " + Math.round(s.uptime_seconds) + "s";
  var r = document.getElementById("rates"); r.textContent = "";
  ["retries", "gave_up", "rejections", "timeouts"].forEach(function (k) { var c = el("div", "", "card"); c.appendChild(el("b", s.rates[k] || 0)); c.appendChild(el("span", k.replace("_", " "), "mut")); r.appendChild(c); });
  fill("breakers", ["breaker", "state"], Object.keys(s.breakers).sort().map(function (n) { return [n, { node: el("span", s.breakers[n], "pill " + s.breakers[n]) }]; }));
  var bulk = [], hl = [];
  Object.keys(s.health).sort().forEach(function (n) { var h = s.health[n];
    hl.push([n, { node: el("span", h.healthy ? "up" : "down", "pill " + (h.healthy ? "up" : "down")) }, h.state || h.error || ""]);
    if (typeof h.active === "number") { var w = Math.min(h.active * 10, 120); var cell = el("span"); var b = el("span", "", "bar"); b.style.width = w + "px"; cell.appendChild(b); cell.appendChild(el("span", " " + h.active + " active", "mut")); bulk.push([n, { node: cell }, h.waiting]); } });
  fill("bulkheads", ["bulkhead", "active", "waiting"], bulk);
  fill("health", ["component", "healthy", "detail"], hl);
  fill("functions", ["function", "retries", "gave up", "timeouts", "rejections", "cache hits", "cache misses"], Object.keys(s.functions).sort().map(function (n) { var f = s.functions[n]; return [n, f.retries, f.gave_up, f.timeouts, f.rejections, f.cache_hits, f.cache_misses]; }));
  fill("events", ["time", "event", "function", "fields"], s.events.slice(-50).reverse().map(function (e) { return [new Date(e.time * 1000).toLocaleTimeString(), e.event, e.function, JSON.stringify(e.fields)]; }));
}
function tick() {
  fetch(ENDPOINT, { cache: "no-store" }).then(function (x) { return x.json(); }).then(render)
    .catch(function () { document.getElementById("status").textContent = "disconnected"; });
}
tick(); setInterval(tick, 2000);
</script>
</body>
</html>
"""  # noqa: E501
