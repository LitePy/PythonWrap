"""Opt-in OpenTelemetry and Sentry adapters for decorator events.

Labels have bounded cardinality and never carry exception messages, arguments or
results: exceptions are reduced to their type names.
"""

from __future__ import annotations

import time
from typing import Any

from python_wrap import _fork
from python_wrap.events import Event, subscribe

__all__ = [
    "COUNTERS",
    "Breadcrumbs",
    "GenAITelemetry",
    "Telemetry",
    "annotate_span",
    "enable",
    "enable_genai",
    "genai_span_attributes",
    "sentry_breadcrumbs",
]
# One table of event -> counter, used by `Telemetry` and checked against the Prometheus
# exporter in tests so the two cannot drift. Each row: legacy name, OpenTelemetry-style
# name, unit, description.
COUNTERS: dict[str, tuple[str, str, str, str]] = {
    "retry.attempt_failed": (
        "pw_retry_retries_total", "python_wrap.retry.retries", "{retry}",
        "Failed attempts that were followed by a retry.",
    ),
    "retry.gave_up": (
        "pw_retry_gave_up_total", "python_wrap.retry.gave_up", "{call}",
        "Calls whose retries were exhausted.",
    ),
    "retry.budget_exhausted": (
        "pw_retry_budget_exhausted_total", "python_wrap.retry.budget_exhausted", "{call}",
        "Calls stopped because the retry budget was used up.",
    ),
    "cache.hit": ("pw_cache_hits_total", "python_wrap.cache.hits", "{hit}", "Cache hits."),
    "cache.miss": ("pw_cache_misses_total", "python_wrap.cache.misses", "{miss}", "Cache misses."),
    "bulkhead.rejected": (
        "pw_bulkhead_rejected_total", "python_wrap.bulkhead.rejected", "{call}",
        "Calls refused by a full bulkhead.",
    ),
    "circuit.rejected": (
        "pw_breaker_rejected_total", "python_wrap.circuit.rejected", "{call}",
        "Calls refused by an open circuit breaker.",
    ),
    "rate_limit.rejected": (
        "pw_rate_limit_rejected_total", "python_wrap.rate_limit.rejected", "{call}",
        "Calls refused by a rate limiter.",
    ),
    "timeout.expired": (
        "pw_timeouts_total", "python_wrap.timeouts", "{call}", "Calls that hit their time limit."
    ),
    "backend.error": (
        "pw_backend_errors_total", "python_wrap.backend.errors", "{error}",
        "Errors from a shared backend (function is the backend name).",
    ),
    "rate_limit.store_error": (
        "pw_rate_limit_store_errors_total", "python_wrap.rate_limit.store_errors", "{error}",
        "Errors from a shared token window's storage.",
    ),
}  # fmt: skip
WAIT = ("pw_limiter_wait_seconds", "python_wrap.rate_limit.wait")
BREAKER_STATE = ("pw_breaker_state", "python_wrap.circuit.state")
HEALTH_UP = ("pw_health_up", "python_wrap.health.up")
_STATES = {"closed": 0, "half_open": 1, "open": 2}
SPAN_MODES = ("events", "notifications", "off")


class Telemetry:
    """A subscription handle. Close removes instrumentation without changing providers.

    Metrics: one counter per `COUNTERS` row, a histogram of rate-limiter waits, and two
    observable gauges read at each collection: every breaker's state (0 closed,
    1 half-open, 2 open; a shared breaker reports its state as this process sees it, so
    every process converges within ``state_ttl``) and each health component's ``up``.

    Traces: ``spans="events"`` (default) adds each event as a span event on the current
    span; ``"notifications"`` opens and closes a ``pw.<event>`` span per event (the
    pre-0.4.0b2 behaviour); ``"off"`` adds nothing. ``attempt_spans=True`` records one
    span per retry attempt with its real duration and attempt number;
    ``wait_spans=True`` one span per rate-limiter wait.
    """

    def __init__(
        self,
        tracer: Any,
        meter: Any,
        *,
        max_functions: int = 100,
        spans: str = "events",
        attempt_spans: bool = False,
        wait_spans: bool = False,
        naming: str = "legacy",
        current_span: Any = None,
        registry: Any = None,
    ) -> None:
        if max_functions < 1:
            raise ValueError("max_functions must be positive")
        if spans not in SPAN_MODES:
            raise ValueError(f"spans must be one of {SPAN_MODES}")
        if naming not in ("legacy", "otel"):
            raise ValueError("naming must be 'legacy' or 'otel'")
        self.tracer = tracer
        self.limit = max_functions
        self.spans, self.attempt_spans, self.wait_spans = spans, attempt_spans, wait_spans
        self._current_span = current_span
        self._names: set[str] = set()
        self._lock = _fork.ForkSafeLock()
        pick = 0 if naming == "legacy" else 1
        self._counters: dict[str, Any] = {}
        self._waits = self._states = self._health = None
        if meter:
            for event, row in COUNTERS.items():
                if pick:
                    self._counters[event] = meter.create_counter(
                        row[1], unit=row[2], description=row[3]
                    )
                else:
                    self._counters[event] = meter.create_counter(row[0])
            self._waits = meter.create_histogram(WAIT[pick], unit="s")
            self._states = meter.create_observable_gauge(
                BREAKER_STATE[pick], callbacks=[self._observe_breakers]
            )
            self._registry = registry
            if registry is not None:
                self._health = meter.create_observable_gauge(
                    HEALTH_UP[pick], callbacks=[self._observe_health]
                )
        self._unsubscribe = subscribe(self._event)
        self.closed = False

    def _label(self, name: str) -> str:
        with self._lock:
            if name not in self._names:
                if len(self._names) >= self.limit:
                    return "_other"
                self._names.add(name)
        return name

    def _observe_breakers(self, options: Any = None) -> Any:
        from opentelemetry.metrics import Observation

        from python_wrap.circuit import _BREAKERS

        if self.closed:
            return []
        out = []
        for breaker in list(_BREAKERS):
            try:
                state = breaker.state.value
            except Exception:  # a store error must not break the whole collection
                continue
            out.append(Observation(_STATES[state], {"function": self._label(breaker.name)}))
        return out

    def _observe_health(self, options: Any = None) -> Any:
        from opentelemetry.metrics import Observation

        if self.closed:
            return []
        report = self._registry.report()
        return [
            Observation(1 if info.get("healthy") else 0, {"component": component})
            for component, info in sorted(report.items())
        ]

    def _span_back(self, name: str, seconds: Any, attributes: dict[str, Any]) -> None:
        """A span that ended now and lasted ``seconds`` (skipped when unknown)."""
        if self.tracer is None or not isinstance(seconds, (int, float)) or seconds <= 0:
            return
        end = time.time_ns()
        span = self.tracer.start_span(
            name, start_time=end - int(seconds * 1e9), attributes=attributes
        )
        span.end(end_time=end)

    def _event(self, event: Event) -> None:
        name = self._label(event.function)
        labels = {"function": name}
        if self.tracer is not None and self.spans == "notifications":
            with self.tracer.start_as_current_span("pw." + event.name) as span:
                span.set_attribute("python_wrap.function", name)
                # Exception messages, args, result payloads and context are never labels.
        elif self._current_span is not None and self.spans == "events":
            span = self._current_span()
            if span.is_recording():
                span.add_event("pw." + event.name, {"python_wrap.function": name})
        counter = self._counters.get(event.name)
        if counter is not None:
            counter.add(1, labels)
        fields = event.fields
        if event.name == "rate_limit.waited":
            if self._waits is not None:
                self._waits.record(float(fields["seconds"]), labels)
            if self.wait_spans:
                self._span_back(
                    "python_wrap.rate_limit.wait",
                    fields.get("seconds"),
                    {"python_wrap.function": name},
                )
        if self.attempt_spans and event.name in _ATTEMPT_EVENTS:
            attempt, outcome = _ATTEMPT_EVENTS[event.name](fields)
            self._span_back(
                "python_wrap.retry.attempt",
                fields.get("attempt_seconds"),
                {
                    "python_wrap.function": name,
                    "python_wrap.retry.attempt": attempt,
                    "python_wrap.retry.outcome": outcome,
                },
            )

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._unsubscribe()

    def __enter__(self) -> Telemetry:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


_ATTEMPT_EVENTS: dict[str, Any] = {
    "retry.attempt_failed": lambda f: (f.get("attempt", 0), "retry"),
    "retry.gave_up": lambda f: (f.get("attempts", 0), "gave_up"),
    "retry.succeeded": lambda f: (f.get("attempts", 0), "success"),
}


def enable(
    *,
    tracer_provider: Any = None,
    meter_provider: Any = None,
    max_functions: int = 100,
    spans: str = "events",
    attempt_spans: bool = False,
    wait_spans: bool = False,
    naming: str = "legacy",
    registry: Any = None,
) -> Telemetry | None:
    """Subscribe using supplied or global OpenTelemetry providers; no-op if absent.

    See `Telemetry` for the instruments and span modes. ``naming="otel"`` uses
    OpenTelemetry-style names with units (``python_wrap.retry.gave_up``); the default
    ``"legacy"`` keeps the ``pw_*`` names until a minor release changes it. ``registry``
    (default: `python_wrap.health.health`) feeds the component health gauge; pass
    ``False`` to leave it out. No provider is installed or shut down by this helper, and
    service attributes come from the provider's resource. Metrics never use
    request/user/cache-key labels; the only label is a bounded ``function``.
    """
    try:
        from opentelemetry import metrics, trace
    except ImportError:
        return None
    if registry is None:
        from python_wrap.health import health as registry
    tracer = (tracer_provider or trace).get_tracer("python_wrap")
    meter = (meter_provider or metrics).get_meter("python_wrap")
    return Telemetry(
        tracer,
        meter,
        max_functions=max_functions,
        spans=spans,
        attempt_spans=attempt_spans,
        wait_spans=wait_spans,
        naming=naming,
        current_span=trace.get_current_span,
        registry=None if registry is False else registry,
    )


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
