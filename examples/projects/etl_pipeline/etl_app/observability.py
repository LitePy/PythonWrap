"""Metrics and health: what an on-call person looks at in the morning."""

from __future__ import annotations

from typing import Any

from python_wrap.exporters import PrometheusExporter
from python_wrap.health import HealthRegistry

from .pipeline import Pipeline


def build_observability(pipeline: Pipeline) -> tuple[PrometheusExporter, HealthRegistry]:
    """A private registry (so tests do not share state) with one entry per source bulkhead."""
    registry = HealthRegistry()
    for name, bulkhead in pipeline.bulkheads.items():
        registry.register(f"bulkhead-{name}", bulkhead)
        # A source is healthy unless its last run ended in an error or a timeout.
        registry.register_check(f"source-{name}", lambda name=name: pipeline.source_ok(name))
    registry.register_check("warehouse", lambda: pipeline.warehouse is not None)
    exporter = PrometheusExporter(namespace="etl", registry=registry).install()
    return exporter, registry


def health_summary(registry: HealthRegistry) -> dict[str, Any]:
    # Only the warehouse is critical: a stuck source degrades the service, it does not stop it.
    return registry.summary(critical=["warehouse"])
