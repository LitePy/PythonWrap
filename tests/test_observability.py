from __future__ import annotations

from typing import Any

from python_wrap import memoize
from python_wrap.observability import Telemetry


class Instrument:
    def __init__(self):
        self.values = []

    def add(self, value, attributes):
        self.values.append((value, attributes))

    record = add
    set = add


class Meter:
    def __init__(self):
        self.metrics = {}

    def create_counter(self, name, **kw):
        self.metrics[name] = Instrument()
        return self.metrics[name]

    def create_histogram(self, name, **kw):
        return self.create_counter(name)

    def create_observable_gauge(self, name, callbacks=(), **kw):
        self.metrics[name] = callbacks
        return callbacks


def test_event_metrics_are_opt_in_and_cardinality_bounded():
    meter = Meter()
    handle = Telemetry(None, meter, max_functions=1)

    @memoize
    def first(secret):
        return secret

    @memoize
    def second(secret):
        return secret

    first("private-user-1")
    first("private-user-1")
    second("private-user-2")
    hits = meter.metrics["pw_cache_hits_total"].values
    assert hits == [(1, {"function": first.__qualname__})]
    misses = meter.metrics["pw_cache_misses_total"].values
    assert misses[-1] == (1, {"function": "_other"})
    assert "private-user" not in repr(meter.metrics["pw_cache_misses_total"].values)
    handle.close()
    second("private-user-2")
    assert len(hits) == 1


def test_real_otel_sdk_event_spans_and_measurements():
    import pytest

    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from python_wrap.observability import enable

    exporter = InMemorySpanExporter()
    tracing = TracerProvider()
    tracing.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    metrics = MeterProvider(metric_readers=[reader])

    @memoize
    def work(value):
        return value

    handle = enable(tracer_provider=tracing, meter_provider=metrics, spans="notifications")
    try:
        work(1)
        work(1)
        assert {span.name for span in exporter.get_finished_spans()} == {
            "pw.cache.miss",
            "pw.cache.hit",
        }
        data = reader.get_metrics_data()
        names = {
            metric.name
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        assert {"pw_cache_hits_total", "pw_cache_misses_total"} <= names
    finally:
        handle.close()
        metrics.shutdown()
        tracing.shutdown()


def _otel():
    import pytest

    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    tracing = TracerProvider()
    tracing.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    return exporter, tracing, reader, MeterProvider(metric_readers=[reader])


def _points(reader, name):
    return [
        point
        for resource in reader.get_metrics_data().resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


def test_gave_up_counter_and_attempt_spans_with_durations(fake_clock: Any) -> None:
    import pytest

    from python_wrap import retry
    from python_wrap.observability import enable

    exporter, tracing, reader, metrics = _otel()

    @retry(max_attempts=3, delay=0)
    def flaky():
        fake_clock.advance(0.002)
        raise ConnectionError

    handle = enable(
        tracer_provider=tracing, meter_provider=metrics, attempt_spans=True, naming="otel"
    )
    try:
        with (
            tracing.get_tracer("t").start_as_current_span("request"),
            pytest.raises(ConnectionError),
        ):
            flaky()
        spans = exporter.get_finished_spans()
        attempts = [s for s in spans if s.name == "python_wrap.retry.attempt"]
        assert [s.attributes["python_wrap.retry.attempt"] for s in attempts] == [1, 2, 3]
        assert all(s.end_time > s.start_time for s in spans)  # no zero-length span
        request = next(s for s in spans if s.name == "request")
        assert any(e.name == "pw.retry.gave_up" for e in request.events)
        assert sum(p.value for p in _points(reader, "python_wrap.retry.gave_up")) == 1
    finally:
        handle.close()
        metrics.shutdown()
        tracing.shutdown()


def test_breaker_gauge_reads_shared_state_in_every_process(tmp_path):
    from python_wrap.circuit import CircuitBreaker
    from python_wrap.local_state import SQLiteBreakerState
    from python_wrap.observability import enable

    _, tracing, reader, metrics = _otel()
    db = tmp_path / "breakers.db"
    here = CircuitBreaker(name="shared-gauge", backend=SQLiteBreakerState(db))
    elsewhere = CircuitBreaker(name="shared-gauge", backend=SQLiteBreakerState(db))
    handle = enable(tracer_provider=tracing, meter_provider=metrics, registry=False)
    try:
        elsewhere.force_open()  # another process opens it; no event reaches `here`
        values = {
            p.value
            for p in _points(reader, "pw_breaker_state")
            if p.attributes["function"] == "shared-gauge"
        }
        assert values == {2}
        assert here.state.value == "open"
    finally:
        handle.close()
        metrics.shutdown()
        tracing.shutdown()


def test_otel_counters_cover_every_prometheus_counter():
    import inspect

    from python_wrap import exporters
    from python_wrap.observability import COUNTERS

    source = inspect.getsource(exporters.PrometheusExporter._on_event)
    prometheus_events = {"retry.attempt_failed", "retry.gave_up", "timeout.expired"}
    prometheus_events |= set(exporters._REJECTION_KINDS) | {"cache.hit", "cache.miss"}
    for event in prometheus_events:
        assert event in source or event in exporters._REJECTION_KINDS
        assert event in COUNTERS, event
