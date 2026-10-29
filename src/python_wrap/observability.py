"""Opt-in OpenTelemetry adapters for decorator events, with bounded label cardinality."""

from __future__ import annotations

from typing import Any

from python_wrap import _fork
from python_wrap.events import Event, subscribe

__all__ = ["Telemetry", "enable"]
_COUNTERS = {
    "retry.attempt_failed": "pw_retry_retries_total",
    "cache.hit": "pw_cache_hits_total",
    "cache.miss": "pw_cache_misses_total",
    "bulkhead.rejected": "pw_bulkhead_rejected_total",
    "circuit.rejected": "pw_breaker_rejected_total",
    "timeout.expired": "pw_timeouts_total",
}


class Telemetry:
    """A subscription handle. Close removes instrumentation without changing providers."""

    def __init__(self, tracer: Any, meter: Any, *, max_functions: int = 100) -> None:
        if max_functions < 1:
            raise ValueError("max_functions must be positive")
        self.tracer = tracer
        self.limit = max_functions
        self._names: set[str] = set()
        self._lock = _fork.ForkSafeLock()
        self._counters = (
            {event: meter.create_counter(name) for event, name in _COUNTERS.items()}
            if meter
            else {}
        )
        self._waits = meter.create_histogram("pw_limiter_wait_seconds", unit="s") if meter else None
        self._states = meter.create_gauge("pw_breaker_state") if meter else None
        self._unsubscribe = subscribe(self._event)
        self.closed = False

    def _event(self, event: Event) -> None:
        with self._lock:
            name = event.function
            if name not in self._names:
                if len(self._names) >= self.limit:
                    name = "_other"
                else:
                    self._names.add(name)
        labels = {"function": name}
        if self.tracer is not None:
            with self.tracer.start_as_current_span("pw." + event.name) as span:
                span.set_attribute("python_wrap.function", name)
                # Exception messages, args, result payloads and context are never labels.
        counter = self._counters.get(event.name)
        if counter is not None:
            counter.add(1, labels)
        if self._waits is not None and event.name == "rate_limit.waited":
            self._waits.record(float(event.fields["seconds"]), labels)
        if self._states is not None and event.name == "circuit.state_changed":
            state = getattr(event.fields["new"], "value", event.fields["new"])
            self._states.set({"closed": 0, "half_open": 1, "open": 2}[state], labels)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._unsubscribe()

    def __enter__(self) -> Telemetry:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def enable(
    *, tracer_provider: Any = None, meter_provider: Any = None, max_functions: int = 100
) -> Telemetry | None:
    """Subscribe using supplied or global OpenTelemetry providers; no-op if absent.

    Requires a recent opentelemetry-api with synchronous gauges for meter support.
    Event spans describe notifications, not the duration of an underlying operation;
    use trace(otel=True) for operation-duration spans. No provider is installed or shut
    down by this helper. Metrics never use request/user/cache-key labels.
    """
    try:
        from opentelemetry import metrics, trace
    except ImportError:
        return None
    tracer = (tracer_provider or trace).get_tracer("python_wrap")
    meter = (meter_provider or metrics).get_meter("python_wrap")
    return Telemetry(tracer, meter, max_functions=max_functions)
