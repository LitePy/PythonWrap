from __future__ import annotations

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

    def create_counter(self, name):
        self.metrics[name] = Instrument()
        return self.metrics[name]

    def create_histogram(self, name, **kw):
        return self.create_counter(name)

    create_gauge = create_counter


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

    handle = enable(tracer_provider=tracing, meter_provider=metrics)
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
