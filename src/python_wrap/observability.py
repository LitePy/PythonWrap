"""Opt-in OpenTelemetry and Sentry adapters for decorator events.

Labels have bounded cardinality and never carry exception messages, arguments or
results: exceptions are reduced to their type names.
"""

from __future__ import annotations

from typing import Any

from python_wrap import _fork
from python_wrap.events import Event, subscribe

__all__ = ["Breadcrumbs", "Telemetry", "annotate_span", "enable", "sentry_breadcrumbs"]
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


def _safe(value: Any) -> Any:
    """Scalars pass through; exceptions and other objects become their type name."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    state = getattr(value, "value", None)  # enums such as CircuitState
    if isinstance(state, str):
        return state
    return type(value).__name__


_LEVELS = {
    "retry.gave_up": "error",
    "retry.budget_exhausted": "warning",
    "circuit.state_changed": "warning",
    "circuit.rejected": "warning",
    "timeout.expired": "warning",
    "bulkhead.rejected": "warning",
    "rate_limit.rejected": "warning",
    "periodic.failed": "error",
}


class Breadcrumbs:
    """Subscription handle returned by `sentry_breadcrumbs`; `close()` stops recording."""

    def __init__(self, sentry: Any, prefixes: tuple[str, ...]) -> None:
        self.sentry, self.prefixes = sentry, prefixes
        self._unsubscribe = subscribe(self._event)
        self.closed = False

    def _event(self, event: Event) -> None:
        if not event.name.startswith(self.prefixes):
            return
        data = {key: _safe(value) for key, value in event.fields.items()}
        data["function"] = event.function
        self.sentry.add_breadcrumb(
            category="python_wrap",
            message=f"{event.name} {event.function}",
            level=_LEVELS.get(event.name, "info"),
            data=data,
        )

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._unsubscribe()

    def __enter__(self) -> Breadcrumbs:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def sentry_breadcrumbs(
    *,
    sentry: Any = None,
    prefixes: tuple[str, ...] = ("retry.", "circuit.", "timeout.", "bulkhead.", "rate_limit."),
) -> Breadcrumbs | None:
    """Record python-wrap events as Sentry breadcrumbs, so an error report shows its retries.

    Each event becomes a breadcrumb in category ``python_wrap`` (gave-up and failed
    periodic runs at level ``error``, rejections and timeouts at ``warning``). Exception
    fields are reduced to type names; messages, arguments and results are never sent.

    Args:
        sentry: The ``sentry_sdk`` module or any object with ``add_breadcrumb``; by
            default ``sentry_sdk`` is imported if installed.
        prefixes: Event name prefixes to record.

    Returns:
        A `Breadcrumbs` handle, or None when ``sentry_sdk`` is not installed.
    """
    if sentry is None:
        try:
            import importlib

            sentry = importlib.import_module("sentry_sdk")
        except ImportError:
            return None
    return Breadcrumbs(sentry, prefixes)


def annotate_span(span: Any, exc: BaseException) -> None:
    """Add a re-raised exception's retry timeline to an OpenTelemetry-style span.

    Sets ``python_wrap.attempts`` and, per attempt ``n``, ``python_wrap.attempt.<n>.exception``
    and ``.duration``; adds one ``python_wrap.retry`` span event per attempt. Does nothing
    for exceptions that did not come out of `retry`.

    Args:
        span: An object with ``set_attribute`` (and optionally ``add_event``).
        exc: The exception being recorded.
    """
    from python_wrap.retry import attempt_history

    history = attempt_history(exc)
    if not history:
        return
    span.set_attribute("python_wrap.attempts", len(history))
    add_event = getattr(span, "add_event", None)
    for record in history:
        prefix = f"python_wrap.attempt.{record.number}"
        span.set_attribute(prefix + ".exception", record.exception)
        span.set_attribute(prefix + ".duration", record.duration)
        if add_event is not None:
            attributes: dict[str, Any] = {
                "attempt": record.number,
                "exception": record.exception,
                "duration": record.duration,
            }
            if record.next_delay is not None:
                attributes["next_delay"] = record.next_delay
            add_event("python_wrap.retry", attributes)
