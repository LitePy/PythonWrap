from __future__ import annotations

import asyncio
import socket
import sys
import types
from typing import Any

import pytest

from python_wrap import _core
from python_wrap.exporters import CONTENT_TYPE, PrometheusExporter, StatsDExporter
from python_wrap.health import HealthRegistry


def feed() -> None:
    _core.emit(
        "retry.attempt_failed", "svc.call", attempt=1, exception=ValueError("x"), next_delay=0.3
    )
    _core.emit("retry.attempt_failed", "svc.call", attempt=2, exception=None, next_delay=2.0)
    _core.emit("retry.gave_up", "svc.call", attempts=3, exception=None)
    _core.emit("timeout.expired", "svc.call", seconds=1.0)
    _core.emit("circuit.rejected", "db", retry_after=1.0)
    _core.emit("bulkhead.rejected", "pool", active=2, waiting=1)
    _core.emit("cache.hit", "svc.load")
    _core.emit("cache.miss", "svc.load")
    _core.emit("circuit.state_changed", "db", old="closed", new="open")


EXPECTED = """\
# HELP t_cache_hits_total Cache hits.
# TYPE t_cache_hits_total counter
t_cache_hits_total{function="svc.load"} 1
# HELP t_cache_misses_total Cache misses.
# TYPE t_cache_misses_total counter
t_cache_misses_total{function="svc.load"} 1
# HELP t_circuit_state Circuit breaker state: 0 closed, 1 half_open, 2 open.
# TYPE t_circuit_state gauge
t_circuit_state{breaker="db"} 2
# HELP t_dropped_series_total Label sets dropped because max_series was reached.
# TYPE t_dropped_series_total counter
t_dropped_series_total 0
# HELP t_events_total Events published by python_wrap decorators.
# TYPE t_events_total counter
t_events_total{event="bulkhead.rejected",function="pool"} 1
t_events_total{event="cache.hit",function="svc.load"} 1
t_events_total{event="cache.miss",function="svc.load"} 1
t_events_total{event="circuit.rejected",function="db"} 1
t_events_total{event="circuit.state_changed",function="db"} 1
t_events_total{event="retry.attempt_failed",function="svc.call"} 2
t_events_total{event="retry.gave_up",function="svc.call"} 1
t_events_total{event="timeout.expired",function="svc.call"} 1
# HELP t_rejections_total Calls refused by a circuit breaker, rate limiter or bulkhead.
# TYPE t_rejections_total counter
t_rejections_total{kind="bulkhead",function="pool"} 1
t_rejections_total{kind="circuit",function="db"} 1
# HELP t_retry_attempts_total Failed attempts that were followed by a retry.
# TYPE t_retry_attempts_total counter
t_retry_attempts_total{function="svc.call"} 2
# HELP t_retry_gave_up_total Calls whose retries were exhausted.
# TYPE t_retry_gave_up_total counter
t_retry_gave_up_total{function="svc.call"} 1
# HELP t_retry_wait_seconds Delay before the next retry attempt in seconds.
# TYPE t_retry_wait_seconds histogram
t_retry_wait_seconds_bucket{le="0.5"} 1
t_retry_wait_seconds_bucket{le="1"} 1
t_retry_wait_seconds_bucket{le="2.5"} 2
t_retry_wait_seconds_bucket{le="+Inf"} 2
t_retry_wait_seconds_sum 2.3
t_retry_wait_seconds_count 2
# HELP t_timeouts_total Calls that exceeded their time limit.
# TYPE t_timeouts_total counter
t_timeouts_total{function="svc.call"} 1
"""


def test_render_exact_text() -> None:
    exporter = PrometheusExporter(namespace="t", registry=None, buckets=(0.5, 1, 2.5)).install()
    feed()
    text = exporter.render()
    exporter.close()
    assert text == EXPECTED


def test_close_unsubscribes() -> None:
    exporter = PrometheusExporter(registry=None).install()
    exporter.install()  # idempotent
    _core.emit("cache.hit", "f")
    exporter.close()
    exporter.close()
    _core.emit("cache.hit", "f")
    assert 'python_wrap_cache_hits_total{function="f"} 1\n' in exporter.render()
    assert not _core.listeners


def test_context_manager_installs_and_closes() -> None:
    with PrometheusExporter(registry=None) as exporter:
        _core.emit("cache.miss", "f")
    _core.emit("cache.miss", "f")
    assert 'python_wrap_cache_misses_total{function="f"} 1\n' in exporter.render()


def test_label_escaping() -> None:
    with PrometheusExporter(registry=None) as exporter:
        _core.emit("cache.hit", 'a\\b"c\nd')
    assert 'python_wrap_cache_hits_total{function="a\\\\b\\"c\\nd"} 1\n' in exporter.render()


def test_cardinality_cap_drops_and_counts() -> None:
    with PrometheusExporter(registry=None, max_series=3) as exporter:
        for i in range(5):
            _core.emit("cache.hit", f"f{i}")
    text = exporter.render()
    # f0 takes events+hits, f1 takes events; every later series is dropped
    assert text.count("python_wrap_events_total{") == 2
    assert text.count("python_wrap_cache_hits_total{") == 1
    dropped = [
        line for line in text.splitlines() if line.startswith("python_wrap_dropped_series_total ")
    ]
    assert dropped == ["python_wrap_dropped_series_total 7"]
    with exporter:
        _core.emit("cache.hit", "f0")
    assert 'python_wrap_events_total{event="cache.hit",function="f0"} 2' in exporter.render()


def test_circuit_gauge_transitions() -> None:
    with PrometheusExporter(registry=None) as exporter:
        for new, value in (("open", 2), ("half_open", 1), ("closed", 0)):
            _core.emit("circuit.state_changed", "db", old="x", new=new)
            assert f'python_wrap_circuit_state{{breaker="db"}} {value}\n' in exporter.render()


def test_histogram_buckets_cumulative_and_overflow() -> None:
    with PrometheusExporter(registry=None, buckets=(1, 5)) as exporter:
        for seconds in (0.5, 1.0, 3.0, 99.0):
            _core.emit("periodic.run", "job", run=1, seconds=seconds)
    text = exporter.render()
    assert 'python_wrap_periodic_run_seconds_bucket{function="job",le="1"} 2\n' in text
    assert 'python_wrap_periodic_run_seconds_bucket{function="job",le="5"} 3\n' in text
    assert 'python_wrap_periodic_run_seconds_bucket{function="job",le="+Inf"} 4\n' in text
    assert 'python_wrap_periodic_run_seconds_sum{function="job"} 103.5\n' in text
    assert 'python_wrap_periodic_run_seconds_count{function="job"} 4\n' in text


def test_invalid_arguments() -> None:
    with pytest.raises(ValueError):
        PrometheusExporter(namespace="bad-name")
    with pytest.raises(ValueError):
        PrometheusExporter(buckets=(2, 1))
    with pytest.raises(ValueError):
        PrometheusExporter(buckets=())
    with pytest.raises(ValueError):
        PrometheusExporter(max_series=0)


def test_live_health_gauges() -> None:
    class Pool:
        active = 3
        waiting = 2

    pool = Pool()
    registry = HealthRegistry()
    registry.register("pool", pool)
    registry.register_check("db", lambda: False)
    exporter = PrometheusExporter(registry=registry)
    text = exporter.render()
    assert 'python_wrap_health_up{component="db"} 0\n' in text
    assert 'python_wrap_health_up{component="pool"} 1\n' in text
    assert 'python_wrap_bulkhead_active{component="pool"} 3\n' in text
    assert 'python_wrap_bulkhead_waiting{component="pool"} 2\n' in text
    pool.active = 1
    assert 'python_wrap_bulkhead_active{component="pool"} 1\n' in exporter.render()


def test_snapshot_is_plain_data() -> None:
    with PrometheusExporter(registry=None) as exporter:
        _core.emit("retry.attempt_failed", "f", attempt=1, exception=None, next_delay=1.0)
    snap = exporter.snapshot()
    assert snap["python_wrap_retry_attempts_total"] == [{"labels": {"function": "f"}, "value": 1}]
    assert snap["python_wrap_retry_wait_seconds"][0]["count"] == 1


def test_wsgi_app() -> None:
    with PrometheusExporter(registry=None) as exporter:
        _core.emit("cache.hit", "f")
    seen: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        seen["status"], seen["headers"] = status, dict(headers)

    body = b"".join(
        exporter.wsgi_app({"PATH_INFO": "/metrics", "REQUEST_METHOD": "GET"}, start_response)
    )
    assert seen["status"] == "200 OK"
    assert (
        seen["headers"]["Content-Type"]
        == CONTENT_TYPE
        == "text/plain; version=0.0.4; charset=utf-8"
    )
    assert seen["headers"]["Content-Length"] == str(len(body))
    assert b'python_wrap_cache_hits_total{function="f"} 1' in body
    exporter.wsgi_app({"PATH_INFO": "/other", "REQUEST_METHOD": "GET"}, start_response)
    assert seen["status"].startswith("404")
    exporter.wsgi_app({"PATH_INFO": "/metrics", "REQUEST_METHOD": "POST"}, start_response)
    assert seen["status"].startswith("405")
    assert (
        exporter.wsgi_app({"PATH_INFO": "/metrics", "REQUEST_METHOD": "HEAD"}, start_response) == []
    )


def run_asgi(app: Any, scope: dict[str, Any]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent


def test_asgi_app() -> None:
    with PrometheusExporter(registry=None) as exporter:
        _core.emit("cache.hit", "f")
    sent = run_asgi(exporter.asgi_app, {"type": "http", "method": "GET", "path": "/metrics"})
    assert sent[0]["status"] == 200
    assert (b"content-type", CONTENT_TYPE.encode()) in sent[0]["headers"]
    assert b'python_wrap_cache_hits_total{function="f"} 1' in sent[1]["body"]
    assert (
        run_asgi(exporter.asgi_app, {"type": "http", "method": "GET", "path": "/x"})[0]["status"]
        == 404
    )


def test_asgi_lifespan() -> None:
    exporter = PrometheusExporter(registry=None)
    messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(exporter.asgi_app({"type": "lifespan"}, receive, send))
    assert [m["type"] for m in sent] == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


def test_prometheus_client_bridge_with_fake_module(monkeypatch: pytest.MonkeyPatch) -> None:
    class Family:
        def __init__(self, name: str, doc: str, labels: list[str] | None = None) -> None:
            self.name, self.doc, self.labels, self.rows = name, doc, labels or [], []

        def add_metric(self, label_values: list[str], *args: Any) -> None:
            self.rows.append((label_values, *args))

    class Registry:
        def __init__(self) -> None:
            self.collectors: list[Any] = []

        def register(self, collector: Any) -> None:
            self.collectors.append(collector)

    core = types.ModuleType("prometheus_client.core")
    core.CounterMetricFamily = type("CounterMetricFamily", (Family,), {})  # type: ignore[attr-defined]
    core.GaugeMetricFamily = type("GaugeMetricFamily", (Family,), {})  # type: ignore[attr-defined]
    core.HistogramMetricFamily = type("HistogramMetricFamily", (Family,), {})  # type: ignore[attr-defined]
    client = types.ModuleType("prometheus_client")
    client.REGISTRY = Registry()  # type: ignore[attr-defined]
    client.core = core  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "prometheus_client", client)
    monkeypatch.setitem(sys.modules, "prometheus_client.core", core)

    with PrometheusExporter(registry=None, buckets=(1,)) as exporter:
        _core.emit("retry.attempt_failed", "f", attempt=1, exception=None, next_delay=0.5)
        _core.emit("circuit.state_changed", "db", old="closed", new="open")
        collector = exporter.register_with_prometheus_client()
    assert client.REGISTRY.collectors == [collector]  # type: ignore[attr-defined]
    families = {f.name: f for f in collector.collect()}
    assert families["python_wrap_retry_attempts"].rows == [(["f"], 1)]
    assert families["python_wrap_circuit_state"].rows == [(["db"], 2)]
    hist = families["python_wrap_retry_wait_seconds"]
    assert hist.rows == [([], [("1", 1), ("+Inf", 1)], 0.5)]
    assert type(families["python_wrap_dropped_series"]).__name__ == "CounterMetricFamily"


def test_prometheus_client_bridge_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "prometheus_client", None)
    with pytest.raises(ImportError, match="prometheus_client"):
        PrometheusExporter(registry=None).register_with_prometheus_client()


def test_prometheus_client_real_roundtrip() -> None:
    prom = pytest.importorskip("prometheus_client")
    registry = prom.CollectorRegistry()
    with PrometheusExporter(registry=None) as exporter:
        _core.emit("cache.hit", "f")
        exporter.register_with_prometheus_client(registry)
    assert prom.generate_latest(registry).decode().count("python_wrap_cache_hits_total") >= 1


# ---------------------------------------------------------------------------- StatsD


def udp_server() -> tuple[socket.socket, int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(2)
    return server, server.getsockname()[1]


def drain(server: socket.socket, count: int) -> list[str]:
    return [server.recv(4096).decode() for _ in range(count)]


def test_statsd_datagrams() -> None:
    server, port = udp_server()
    with server, StatsDExporter(port=port) as exporter:
        _core.emit("cache.hit", "svc.load")
        _core.emit("retry.attempt_failed", "svc.call", attempt=1, exception=None, next_delay=0.25)
        _core.emit("circuit.state_changed", "db", old="closed", new="half_open")
        got = drain(server, 5)
        assert exporter.sent == 5 and exporter.errors == 0
    assert got == [
        "python_wrap.cache.hit:1|c|#function:svc.load",
        "python_wrap.retry.attempt_failed:1|c|#function:svc.call",
        "python_wrap.retry.wait:250|ms|#function:svc.call",
        "python_wrap.circuit.state_changed:1|c|#function:db",
        "python_wrap.circuit.state:1|g|#breaker:db",
    ]


def test_statsd_no_tags_and_prefix_and_unsubscribe() -> None:
    server, port = udp_server()
    with server:
        exporter = StatsDExporter(port=port, prefix="app", tags="none").install()
        _core.emit("cache.miss", "f:o|o")
        assert drain(server, 1) == ["app.cache.miss:1|c"]
        exporter.close()
        _core.emit("cache.miss", "f")
        server.settimeout(0.2)
        with pytest.raises(TimeoutError):
            server.recv(100)


def test_statsd_tag_sanitised() -> None:
    server, port = udp_server()
    with server, StatsDExporter(port=port):
        _core.emit("cache.hit", "a:b|c,d#e")
        assert drain(server, 1) == ["python_wrap.cache.hit:1|c|#function:a_b_c_d_e"]


def test_statsd_sample_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    server, port = udp_server()
    values = iter([0.9, 0.1])
    monkeypatch.setattr("python_wrap.exporters.random.random", lambda: next(values))
    with server, StatsDExporter(port=port, sample_rate=0.5, tags="none"):
        _core.emit("cache.hit", "f")  # 0.9 -> skipped
        _core.emit("cache.miss", "f")  # 0.1 -> sent
        assert drain(server, 1) == ["python_wrap.cache.miss:1|c|@0.5"]


def test_statsd_errors_are_swallowed_and_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    exporter = StatsDExporter(host="127.0.0.1", port=9).install()

    def boom(*args: Any) -> int:
        raise OSError("down")

    monkeypatch.setattr(socket.socket, "sendto", boom)
    _core.emit("cache.hit", "f")
    exporter.close()
    assert exporter.errors == 1 and exporter.sent == 0


def test_statsd_validation() -> None:
    with pytest.raises(ValueError):
        StatsDExporter(sample_rate=0)
    with pytest.raises(ValueError):
        StatsDExporter(tags="bogus")  # type: ignore[arg-type]
