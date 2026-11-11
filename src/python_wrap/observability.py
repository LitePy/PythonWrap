"""Opt-in OpenTelemetry and Sentry adapters for decorator events.

Labels have bounded cardinality and never carry exception messages, arguments or
results: exceptions are reduced to their type names.
"""

from __future__ import annotations

from typing import Any

from python_wrap import _fork
from python_wrap.events import Event, subscribe

__all__ = [
    "Breadcrumbs",
    "GenAITelemetry",
    "Telemetry",
    "annotate_span",
    "enable",
    "enable_genai",
    "genai_span_attributes",
    "sentry_breadcrumbs",
]
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


def genai_span_attributes(event: Event) -> dict[str, Any]:
    """OpenTelemetry GenAI semantic-convention attributes for a ``genai.*`` event.

    Maps ``gen_ai.operation.name`` (``chat`` or ``execute_tool``), ``gen_ai.system``,
    ``gen_ai.request.model``, ``gen_ai.usage.input_tokens``/``output_tokens`` and
    ``gen_ai.tool.name``; python-wrap extras (run id, step, cost, latency, attempts) use the
    ``python_wrap.`` prefix. Captured content, when present, becomes JSON strings under
    ``gen_ai.input.messages`` / ``gen_ai.output.messages``. ``None`` values are omitted.
    """
    import json

    fields = event.fields
    is_tool = event.name == "genai.tool_call"
    raw: dict[str, Any] = {
        "gen_ai.operation.name": "execute_tool" if is_tool else "chat",
        "gen_ai.system": fields.get("system"),
        "gen_ai.request.model": fields.get("model"),
        "gen_ai.usage.input_tokens": fields.get("input_tokens"),
        "gen_ai.usage.output_tokens": fields.get("output_tokens"),
        "gen_ai.tool.name": fields.get("tool") if is_tool else None,
        "python_wrap.run_id": fields.get("run_id"),
        "python_wrap.step": fields.get("step"),
        "python_wrap.cost": fields.get("cost"),
        "python_wrap.latency": fields.get("latency"),
        "python_wrap.attempts": fields.get("attempts"),
        "error.type": fields.get("error_type"),
    }
    for key, target in (("input", "gen_ai.input.messages"), ("output", "gen_ai.output.messages")):
        if fields.get(key) is not None:
            raw[target] = json.dumps(fields[key], default=str)
    return {key: value for key, value in raw.items() if value is not None}


class GenAITelemetry:
    """Subscription that turns ``genai.*`` events into OpenTelemetry spans."""

    def __init__(self, tracer: Any) -> None:
        self.tracer = tracer
        self._unsubscribe = subscribe(self._event)
        self.closed = False

    def _event(self, event: Event) -> None:
        if event.name not in ("genai.llm_call", "genai.tool_call"):
            return
        fields = event.fields
        attributes = genai_span_attributes(event)
        if event.name == "genai.tool_call":
            name = f"execute_tool {fields.get('tool') or event.function}"
        else:
            name = f"chat {fields.get('model') or event.function}"
        started = float(fields.get("started") or 0.0)
        latency = float(fields.get("latency") or 0.0)
        span = self.tracer.start_span(name, start_time=int(started * 1e9))
        try:
            for key, value in attributes.items():
                span.set_attribute(key, value)
        finally:
            span.end(end_time=int((started + latency) * 1e9))

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._unsubscribe()

    def __enter__(self) -> GenAITelemetry:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def enable_genai(*, tracer: Any = None, tracer_provider: Any = None) -> GenAITelemetry | None:
    """Emit GenAI-convention spans for traced LLM and tool calls; no-op without OpenTelemetry.

    Args:
        tracer: A tracer to use directly (no OpenTelemetry import needed).
        tracer_provider: Provider to take the tracer from instead of the global one.

    Returns:
        A closable `GenAITelemetry`, or None when no tracer was given and
        ``opentelemetry`` is not installed.
    """
    if tracer is None:
        try:
            from opentelemetry import trace
        except ImportError:
            return None
        tracer = (tracer_provider or trace).get_tracer("python_wrap")
    return GenAITelemetry(tracer)
