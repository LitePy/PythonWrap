"""Export the event stream as Prometheus text, prometheus_client collectors or StatsD.

`PrometheusExporter` subscribes to the process-wide event stream
(`python_wrap.events`), aggregates it into counters, gauges and histograms with bounded
label cardinality, and renders the Prometheus text exposition format 0.0.4 using only the
standard library. It can also serve itself as a WSGI or ASGI app, or register as a
collector with the optional ``prometheus_client`` package. `StatsDExporter` sends the same
events as fire-and-forget UDP datagrams.

Labels never include exception messages, arguments, cache keys or other payloads: only
event names, function names, breaker names and fixed kinds.
"""

from __future__ import annotations

import importlib
import itertools
import random
import re
import socket
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from python_wrap import _fork
from python_wrap.events import Event, subscribe
from python_wrap.health import HealthRegistry, health

__all__ = ["CONTENT_TYPE", "PrometheusExporter", "StatsDExporter"]

#: Content type of the Prometheus text exposition format served by the exporter.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)

_STATE_VALUES = {"closed": 0, "half_open": 1, "open": 2}
_REJECTION_KINDS = {
    "circuit.rejected": "circuit",
    "rate_limit.rejected": "rate_limit",
    "bulkhead.rejected": "bulkhead",
}
_NAMESPACE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

Labels = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _Hist:
    """A histogram value: finite upper bounds, cumulative counts (incl. +Inf), sum."""

    bounds: tuple[float, ...]
    cumulative: tuple[int, ...]
    total: float


@dataclass
class _Family:
    name: str
    kind: str  # counter | gauge | histogram
    help: str
    series: list[tuple[Labels, float | _Hist]]


class _Meta:
    """Static description of one metric family and its live series."""

    def __init__(self, kind: str, help_text: str, label_names: tuple[str, ...]) -> None:
        self.kind = kind
        self.help = help_text
        self.label_names = label_names
        self.values: dict[tuple[str, ...], Any] = {}


def _num(value: float) -> str:
    if value != value:
        return "NaN"
    if value in (float("inf"), float("-inf")):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(float(value))


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _fmt_labels(labels: Labels, extra: tuple[tuple[str, str], ...] = ()) -> str:
    items = labels + extra
    if not items:
        return ""
    return "{" + ",".join(f'{k}="{_escape_label(v)}"' for k, v in items) + "}"


def _state_name(value: Any) -> str:
    return str(getattr(value, "value", value))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class PrometheusExporter:
    """Aggregate decorator events into Prometheus metrics.

    Metric reference (``{ns}`` is the namespace):

    * ``{ns}_events_total{event,function}`` counter of every event.
    * ``{ns}_retry_attempts_total{function}``, ``{ns}_retry_gave_up_total{function}``,
      ``{ns}_timeouts_total{function}`` counters.
    * ``{ns}_rejections_total{kind,function}`` counter; kind is ``circuit``,
      ``rate_limit`` or ``bulkhead``.
    * ``{ns}_cache_hits_total{function}`` and ``{ns}_cache_misses_total{function}``.
    * ``{ns}_circuit_state{breaker}`` gauge: 0 closed, 1 half_open, 2 open.
    * ``{ns}_retry_wait_seconds`` histogram of delays before a retry.
    * ``{ns}_periodic_run_seconds{function}`` histogram of periodic run durations.
    * ``{ns}_health_up{component}``, ``{ns}_bulkhead_active{component}`` and
      ``{ns}_bulkhead_waiting{component}`` gauges, read from ``registry`` at render time.
    * ``{ns}_dropped_series_total`` counter of label sets dropped by ``max_series``.

    Args:
        namespace: Metric name prefix.
        registry: `HealthRegistry` read at render time; ``None`` disables live gauges.
        buckets: Upper bounds (seconds) of the histograms, ascending.
        max_series: Maximum number of distinct label sets kept across all metrics. New
            label sets beyond it are dropped and counted.

    Raises:
        ValueError: If ``namespace`` is not a valid metric prefix, ``buckets`` is empty or
            not strictly ascending, or ``max_series`` is below 1.

    Example:
        >>> from python_wrap import _core
        >>> with PrometheusExporter(registry=None) as exporter:
        ...     _core.emit("cache.hit", "load")
        ...     "python_wrap_cache_hits_total" in exporter.render()
        True
    """

    def __init__(
        self,
        *,
        namespace: str = "python_wrap",
        registry: HealthRegistry | None = health,
        buckets: Iterable[float] = DEFAULT_BUCKETS,
        max_series: int = 10_000,
    ) -> None:
        if not _NAMESPACE_RE.match(namespace):
            raise ValueError("namespace must match [a-zA-Z_][a-zA-Z0-9_]*")
        bounds = tuple(float(b) for b in buckets)
        if not bounds or any(a >= b for a, b in itertools.pairwise(bounds)):
            raise ValueError("buckets must be non-empty and strictly ascending")
        if max_series < 1:
            raise ValueError("max_series must be at least 1")
        self.namespace = namespace
        self.registry = registry
        self.buckets = bounds
        self.max_series = max_series
        self._lock = _fork.ForkSafeLock()
        self._series = 0
        self._dropped = 0
        self._unsubscribe: Callable[[], None] | None = None
        ns = namespace
        self._meta: dict[str, _Meta] = {
            f"{ns}_events_total": _Meta(
                "counter", "Events published by python_wrap decorators.", ("event", "function")
            ),
            f"{ns}_retry_attempts_total": _Meta(
                "counter", "Failed attempts that were followed by a retry.", ("function",)
            ),
            f"{ns}_retry_gave_up_total": _Meta(
                "counter", "Calls whose retries were exhausted.", ("function",)
            ),
            f"{ns}_timeouts_total": _Meta(
                "counter", "Calls that exceeded their time limit.", ("function",)
            ),
            f"{ns}_rejections_total": _Meta(
                "counter",
                "Calls refused by a circuit breaker, rate limiter or bulkhead.",
                ("kind", "function"),
            ),
            f"{ns}_cache_hits_total": _Meta("counter", "Cache hits.", ("function",)),
            f"{ns}_cache_misses_total": _Meta("counter", "Cache misses.", ("function",)),
            f"{ns}_circuit_state": _Meta(
                "gauge", "Circuit breaker state: 0 closed, 1 half_open, 2 open.", ("breaker",)
            ),
            f"{ns}_retry_wait_seconds": _Meta(
                "histogram", "Delay before the next retry attempt in seconds.", ()
            ),
            f"{ns}_periodic_run_seconds": _Meta(
                "histogram", "Duration of periodic function runs in seconds.", ("function",)
            ),
        }

    # ------------------------------------------------------------------ lifecycle

    def install(self) -> PrometheusExporter:
        """Subscribe to the event stream. Calling it twice has no extra effect.

        Returns:
            This exporter.
        """
        with self._lock:
            if self._unsubscribe is None:
                self._unsubscribe = subscribe(self._on_event)
        return self

    def close(self) -> None:
        """Unsubscribe. Collected values stay available to `render`."""
        with self._lock:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
        if unsubscribe is not None:
            unsubscribe()

    def __enter__(self) -> PrometheusExporter:
        return self.install()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ recording

    def _slot(self, name: str, labels: tuple[str, ...]) -> tuple[_Meta, bool]:
        """Return (meta, ok); ok is False when the series was dropped. Lock held."""
        meta = self._meta[name]
        if labels in meta.values:
            return meta, True
        if self._series >= self.max_series:
            self._dropped += 1
            return meta, False
        self._series += 1
        return meta, True

    def _inc(self, name: str, labels: tuple[str, ...]) -> None:
        meta, ok = self._slot(name, labels)
        if ok:
            meta.values[labels] = meta.values.get(labels, 0) + 1

    def _set(self, name: str, labels: tuple[str, ...], value: float) -> None:
        meta, ok = self._slot(name, labels)
        if ok:
            meta.values[labels] = value

    def _observe(self, name: str, labels: tuple[str, ...], value: float) -> None:
        meta, ok = self._slot(name, labels)
        if not ok:
            return
        state = meta.values.get(labels)
        if state is None:
            state = [[0] * (len(self.buckets) + 1), 0.0]
            meta.values[labels] = state
        counts: list[int] = state[0]
        for i, bound in enumerate(self.buckets):
            if value <= bound:
                counts[i] += 1
                break
        else:
            counts[-1] += 1
        state[1] += value

    def _on_event(self, event: Event) -> None:
        ns = self.namespace
        name, fn, fields = event.name, event.function, event.fields
        with self._lock:
            self._inc(f"{ns}_events_total", (name, fn))
            if name == "retry.attempt_failed":
                self._inc(f"{ns}_retry_attempts_total", (fn,))
                delay = _number(fields.get("next_delay"))
                if delay is not None and delay >= 0:
                    self._observe(f"{ns}_retry_wait_seconds", (), delay)
            elif name == "retry.gave_up":
                self._inc(f"{ns}_retry_gave_up_total", (fn,))
            elif name == "timeout.expired":
                self._inc(f"{ns}_timeouts_total", (fn,))
            elif name in _REJECTION_KINDS:
                self._inc(f"{ns}_rejections_total", (_REJECTION_KINDS[name], fn))
            elif name == "cache.hit":
                self._inc(f"{ns}_cache_hits_total", (fn,))
            elif name == "cache.miss":
                self._inc(f"{ns}_cache_misses_total", (fn,))
            elif name == "circuit.state_changed":
                state = _STATE_VALUES.get(_state_name(fields.get("new")))
                if state is not None:
                    self._set(f"{ns}_circuit_state", (fn,), state)
            elif name == "periodic.run":
                seconds = _number(fields.get("seconds"))
                if seconds is not None and seconds >= 0:
                    self._observe(f"{ns}_periodic_run_seconds", (fn,), seconds)

    # ------------------------------------------------------------------ reading

    def _collect(self) -> list[_Family]:
        families: list[_Family] = []
        with self._lock:
            for name, meta in self._meta.items():
                series: list[tuple[Labels, float | _Hist]] = []
                for values in sorted(meta.values):
                    labels: Labels = tuple(zip(meta.label_names, values, strict=True))
                    raw = meta.values[values]
                    if meta.kind == "histogram":
                        running = 0
                        cumulative: list[int] = []
                        for count in raw[0]:
                            running += count
                            cumulative.append(running)
                        series.append((labels, _Hist(self.buckets, tuple(cumulative), raw[1])))
                    else:
                        series.append((labels, raw))
                if series:
                    families.append(_Family(name, meta.kind, meta.help, series))
            dropped = self._dropped
        families.append(
            _Family(
                f"{self.namespace}_dropped_series_total",
                "counter",
                "Label sets dropped because max_series was reached.",
                [((), dropped)],
            )
        )
        families.extend(self._health_families())
        families.sort(key=lambda f: f.name)
        return families

    def _health_families(self) -> list[_Family]:
        if self.registry is None:
            return []
        report = self.registry.report()
        ns = self.namespace
        up: list[tuple[Labels, float | _Hist]] = []
        active: list[tuple[Labels, float | _Hist]] = []
        waiting: list[tuple[Labels, float | _Hist]] = []
        for component in sorted(report):
            info = report[component]
            labels: Labels = (("component", component),)
            up.append((labels, 1 if info.get("healthy") else 0))
            a, w = _number(info.get("active")), _number(info.get("waiting"))
            if a is not None and w is not None:
                active.append((labels, a))
                waiting.append((labels, w))
        out: list[_Family] = []
        if up:
            out.append(
                _Family(
                    f"{ns}_health_up", "gauge", "1 when the registered component is healthy.", up
                )
            )
        if active:
            out.append(
                _Family(f"{ns}_bulkhead_active", "gauge", "Calls running in a bulkhead.", active)
            )
            out.append(
                _Family(
                    f"{ns}_bulkhead_waiting", "gauge", "Calls waiting for a bulkhead slot.", waiting
                )
            )
        return out

    def render(self) -> str:
        """Return the metrics in Prometheus text exposition format 0.0.4.

        Output is deterministic: families sorted by name, series by label values.

        Returns:
            The exposition text, ending with a newline.
        """
        lines: list[str] = []
        for fam in self._collect():
            lines.append(f"# HELP {fam.name} {_escape_help(fam.help)}")
            lines.append(f"# TYPE {fam.name} {fam.kind}")
            for labels, value in fam.series:
                if isinstance(value, _Hist):
                    for bound, count in zip(value.bounds, value.cumulative, strict=False):
                        le = (("le", _num(bound)),)
                        lines.append(f"{fam.name}_bucket{_fmt_labels(labels, le)} {count}")
                    inf = (("le", "+Inf"),)
                    lines.append(
                        f"{fam.name}_bucket{_fmt_labels(labels, inf)} {value.cumulative[-1]}"
                    )
                    lines.append(f"{fam.name}_sum{_fmt_labels(labels)} {_num(value.total)}")
                    lines.append(f"{fam.name}_count{_fmt_labels(labels)} {value.cumulative[-1]}")
                else:
                    lines.append(f"{fam.name}{_fmt_labels(labels)} {_num(value)}")
        return "\n".join(lines) + "\n"

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        """Return the metric values as JSON-serialisable data (histograms as count/sum)."""
        out: dict[str, list[dict[str, Any]]] = {}
        for fam in self._collect():
            rows: list[dict[str, Any]] = []
            for labels, value in fam.series:
                row: dict[str, Any] = {"labels": dict(labels)}
                if isinstance(value, _Hist):
                    row["count"] = value.cumulative[-1]
                    row["sum"] = value.total
                else:
                    row["value"] = value
                rows.append(row)
            out[fam.name] = rows
        return out

    # ------------------------------------------------------------------ serving

    def wsgi_app(
        self, environ: Mapping[str, Any], start_response: Callable[..., Any]
    ) -> list[bytes]:
        """WSGI application serving the metrics on ``/metrics`` (or ``/`` when mounted).

        Args:
            environ: The WSGI environ.
            start_response: The WSGI start_response callable.

        Returns:
            The response body chunks.
        """
        path = str(environ.get("PATH_INFO", ""))
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        status, body = self._respond(method, path)
        headers = [("Content-Type", CONTENT_TYPE), ("Content-Length", str(len(body)))]
        start_response(status, headers)
        return [] if method == "HEAD" else [body]

    async def asgi_app(
        self, scope: Mapping[str, Any], receive: Callable[..., Any], send: Callable[..., Any]
    ) -> None:
        """ASGI application serving the metrics on ``/metrics`` (or ``/`` when mounted).

        Args:
            scope: The ASGI scope.
            receive: The ASGI receive callable.
            send: The ASGI send callable.
        """
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
        status, body = self._respond(method, str(scope.get("path", "")))
        code = int(status.split(" ", 1)[0])
        headers = [
            (b"content-type", CONTENT_TYPE.encode("ascii")),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        await send({"type": "http.response.start", "status": code, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})

    def _respond(self, method: str, path: str) -> tuple[str, bytes]:
        if path not in ("", "/", "/metrics"):
            return "404 Not Found", b"not found\n"
        if method not in ("GET", "HEAD"):
            return "405 Method Not Allowed", b"method not allowed\n"
        return "200 OK", self.render().encode("utf-8")

    # ------------------------------------------------------------------ bridge

    def register_with_prometheus_client(self, registry: Any = None) -> Any:
        """Register a collector yielding these metrics with ``prometheus_client``.

        The package is imported lazily; it is not a dependency of python-wrap.

        Args:
            registry: A ``CollectorRegistry``; defaults to ``prometheus_client.REGISTRY``.

        Returns:
            The registered collector (pass it to ``registry.unregister`` to remove it).

        Raises:
            ImportError: If ``prometheus_client`` is not installed.
        """
        try:
            client = importlib.import_module("prometheus_client")
            core = importlib.import_module("prometheus_client.core")
        except ImportError as exc:
            raise ImportError(
                "register_with_prometheus_client needs the prometheus_client package"
            ) from exc
        exporter = self

        class _Collector:
            def collect(self) -> Iterable[Any]:
                for fam in exporter._collect():
                    yield _to_client_family(core, fam)

        collector = _Collector()
        target = registry if registry is not None else client.REGISTRY
        target.register(collector)
        return collector


def _to_client_family(core: Any, fam: _Family) -> Any:
    label_names = [k for k, _ in fam.series[0][0]] if fam.series else []
    if fam.kind == "counter":
        metric = core.CounterMetricFamily(
            fam.name.removesuffix("_total"), fam.help, labels=label_names
        )
        for labels, value in fam.series:
            metric.add_metric([v for _, v in labels], value)
    elif fam.kind == "gauge":
        metric = core.GaugeMetricFamily(fam.name, fam.help, labels=label_names)
        for labels, value in fam.series:
            metric.add_metric([v for _, v in labels], value)
    else:
        metric = core.HistogramMetricFamily(fam.name, fam.help, labels=label_names)
        for labels, value in fam.series:
            assert isinstance(value, _Hist)
            buckets = [(_num(b), c) for b, c in zip(value.bounds, value.cumulative, strict=False)]
            buckets.append(("+Inf", value.cumulative[-1]))
            metric.add_metric([v for _, v in labels], buckets, value.total)
    return metric


_TAG_UNSAFE = re.compile(r"[,|#:\n@]")


def _tag(value: str) -> str:
    return _TAG_UNSAFE.sub("_", value)


class StatsDExporter:
    """Send events to a StatsD or DogStatsD agent over UDP, fire-and-forget.

    Each event becomes a counter ``<prefix>.<event>:1|c``. Durations are also sent as
    timings in milliseconds (``<prefix>.retry.wait``, ``<prefix>.periodic.run``,
    ``<prefix>.rate_limit.wait``, ``<prefix>.timing.slow``) and circuit breaker states as a
    gauge ``<prefix>.circuit.state`` (0 closed, 1 half_open, 2 open). Sending never blocks
    and never raises; failures increase `errors`.

    Args:
        host: Agent host.
        port: Agent UDP port.
        prefix: Metric name prefix.
        tags: ``"dogstatsd"`` appends ``|#function:<name>`` tags; ``"none"`` omits them.
        sample_rate: Fraction of datagrams to send, in (0, 1]. Sampled datagrams carry
            ``|@rate`` so the agent can scale them; gauges are never sampled.

    Raises:
        ValueError: If ``tags`` or ``sample_rate`` is invalid.

    Example:
        >>> exporter = StatsDExporter(port=9, tags="none")
        >>> exporter.close()
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8125,
        *,
        prefix: str = "python_wrap",
        tags: Literal["dogstatsd", "none"] = "dogstatsd",
        sample_rate: float = 1.0,
    ) -> None:
        if tags not in ("dogstatsd", "none"):
            raise ValueError("tags must be 'dogstatsd' or 'none'")
        if not 0 < sample_rate <= 1:
            raise ValueError("sample_rate must be in (0, 1]")
        self.host = host
        self.port = port
        self.prefix = prefix
        self.tags = tags
        self.sample_rate = sample_rate
        self.sent = 0
        self.errors = 0
        self._lock = _fork.ForkSafeLock()
        self._sock: socket.socket | None = None
        self._unsubscribe: Callable[[], None] | None = None

    def install(self) -> StatsDExporter:
        """Subscribe to the event stream. Returns this exporter."""
        with self._lock:
            if self._unsubscribe is None:
                self._unsubscribe = subscribe(self._on_event)
        return self

    def close(self) -> None:
        """Unsubscribe and close the socket."""
        with self._lock:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
            sock, self._sock = self._sock, None
        if unsubscribe is not None:
            unsubscribe()
        if sock is not None:
            sock.close()

    def __enter__(self) -> StatsDExporter:
        return self.install()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _send(self, payload: str) -> None:
        try:
            with self._lock:
                if self._sock is None:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock.setblocking(False)
                    self._sock = sock
                sock = self._sock
            sock.sendto(payload.encode("utf-8"), (self.host, self.port))
            self.sent += 1
        except Exception:
            self.errors += 1

    def _line(
        self, name: str, value: str, kind: str, tag: tuple[str, str] | None, sampled: bool
    ) -> str:
        line = f"{self.prefix}.{name}:{value}|{kind}"
        if sampled and self.sample_rate < 1:
            line += f"|@{self.sample_rate:g}"
        if tag is not None and self.tags == "dogstatsd":
            line += f"|#{tag[0]}:{_tag(tag[1])}"
        return line

    def _on_event(self, event: Event) -> None:
        name, fn, fields = event.name, event.function, event.fields
        function_tag = ("function", fn)
        keep = self.sample_rate >= 1 or random.random() < self.sample_rate
        if keep:
            self._send(self._line(name, "1", "c", function_tag, True))
        timing: tuple[str, Any] | None = None
        if name == "retry.attempt_failed":
            timing = ("retry.wait", fields.get("next_delay"))
        elif name == "periodic.run":
            timing = ("periodic.run", fields.get("seconds"))
        elif name == "rate_limit.waited":
            timing = ("rate_limit.wait", fields.get("seconds"))
        elif name == "timing.slow":
            timing = ("timing.slow", fields.get("seconds"))
        if timing is not None and keep:
            seconds = _number(timing[1])
            if seconds is not None and seconds >= 0:
                self._send(
                    self._line(timing[0], _num(round(seconds * 1000, 3)), "ms", function_tag, True)
                )
        if name == "circuit.state_changed":
            state = _STATE_VALUES.get(_state_name(fields.get("new")))
            if state is not None:
                self._send(self._line("circuit.state", str(state), "g", ("breaker", fn), False))
