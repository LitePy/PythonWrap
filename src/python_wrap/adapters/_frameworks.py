"""Shared pieces of the agent-framework adapters (no framework is imported here)."""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from typing import Any

from python_wrap import _core
from python_wrap.agents import (
    EffectConflict,
    EffectUnknown,
    LoopDetected,
    RunBudgetExceeded,
    RunCancelled,
    RunTimeout,
    StepLimitExceeded,
    canonical,
    run_id_var,
)
from python_wrap.llm import BudgetExceeded, Usage

_LAYER_ATTR = "__python_wrap_layer__"

#: Errors that end the agent run instead of being reported to the model as a tool error.
FATAL: tuple[type[BaseException], ...] = (
    StepLimitExceeded,
    LoopDetected,
    RunTimeout,
    RunCancelled,
    RunBudgetExceeded,
    BudgetExceeded,
    EffectUnknown,
    EffectConflict,
)


def is_model_retry(exc: BaseException) -> bool:
    """True for pydantic-ai's ``ModelRetry`` (matched by class name, never imported)."""
    return any(cls.__name__ == "ModelRetry" for cls in type(exc).__mro__)


class PassThrough(BaseException):
    """Carries an exception through a retry layer that must not retry it."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(repr(error))
        self.error = error


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _field(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def usage_totals(source: Any) -> tuple[int, int] | None:
    """``(input, output)`` tokens of a framework usage object, result or response.

    Reads ``input_tokens`` / ``output_tokens`` (pydantic-ai 1.x, OpenAI Agents SDK),
    ``request_tokens`` / ``response_tokens`` (older pydantic-ai) or ``prompt_tokens`` /
    ``completion_tokens``; a ``usage`` attribute or a ``usage()`` method is followed.
    """
    usage = source
    found = _field(source, "usage")
    if found is not None:
        usage = found() if callable(found) else found
    names = (
        ("input_tokens", "output_tokens"),
        ("request_tokens", "response_tokens"),
        ("prompt_tokens", "completion_tokens"),
    )
    for inputs, outputs in names:
        tin, tout = _field(usage, inputs), _field(usage, outputs)
        if tin is not None or tout is not None:
            return _int(tin), _int(tout)
    return None


class UsageBridge:
    """Feed cumulative framework usage into a python-wrap `Budget`, counting each token once.

    Framework usage objects are running totals for one run. `sync` adds only the growth
    since the last reading; a total that went down means a new run started.
    """

    def __init__(self, budget: Any, model: str | None = None) -> None:
        self.budget = budget
        self.model = model
        self._last = (0, 0)
        self._lock = threading.Lock()

    def add(self, tokens_in: int, tokens_out: int, model: str | None = None) -> Any:
        """Add a known amount (one model response)."""
        if self.budget is None or (tokens_in <= 0 and tokens_out <= 0):
            return None
        return self.budget.record(Usage(max(tokens_in, 0), max(tokens_out, 0), model or self.model))

    def sync(self, source: Any, model: str | None = None) -> Any:
        """Add the growth of a running total; returns the budget's spend (or None)."""
        totals = usage_totals(source)
        if totals is None or self.budget is None:
            return None
        with self._lock:
            last = self._last
            if totals[0] < last[0] or totals[1] < last[1]:
                last = (0, 0)
            delta = (totals[0] - last[0], totals[1] - last[1])
            self._last = totals
        return self.add(delta[0], delta[1], model)

    def reset(self) -> None:
        """Forget the last reading (call between runs that reuse the bridge)."""
        with self._lock:
            self._last = (0, 0)

    def check(self, label: str) -> None:
        """Raise `BudgetExceeded` if the budget's current scope is used up."""
        budget = self.budget
        if budget is None:
            return
        try:
            exhausted = budget.exhausted()
        except (RuntimeError, ValueError):
            return  # a run/key budget used outside its scope: the decorated calls check it
        if exhausted:
            raise budget._refuse(label, budget.spent())


def record_tool(
    tape: Any,
    *,
    framework: str,
    name: str,
    arguments: Any,
    result: Any,
    error: BaseException | None,
    started: float,
    call_id: str | None,
) -> None:
    """Append one tool call to an `AuditTape`; never raises."""
    if tape is None:
        return
    try:
        from python_wrap.genai import Redactor

        scrub = tape.redactor or Redactor()
        tape.record(
            timestamp=time.time(),
            kind="tool",
            name=name,
            function=f"{framework}:{name}",
            run_id=run_id_var.get(),
            tool=name,
            status="ok" if error is None else "error",
            input=scrub({"arguments": arguments, "call_id": call_id}),
            output=None if error is not None else scrub(result),
            error=None
            if error is None
            else {"type": type(error).__name__, "message": scrub(str(error))},
            latency=max(0.0, _core.monotonic() - started),
            content=True,
        )
    except Exception:
        _core.get_logger(None).exception("could not record %s tool call to the tape", framework)


def record_llm(
    tape: Any, *, framework: str, model: str | None, tokens: tuple[int, int] | None
) -> None:
    """Append one model response (metadata only) to an `AuditTape`; never raises."""
    if tape is None:
        return
    try:
        tape.record(
            timestamp=time.time(),
            kind="llm",
            name=model or framework,
            function=f"{framework}:model",
            run_id=run_id_var.get(),
            model=model,
            input_tokens=None if tokens is None else tokens[0],
            output_tokens=None if tokens is None else tokens[1],
        )
    except Exception:
        _core.get_logger(None).exception("could not record %s model call to the tape", framework)


class CallCounter:
    """Tool-call and identical-call limits for frameworks without their own.

    Thread-safe. ``max_tool_calls`` caps every tool call that passes through (sub-agents
    included); ``max_repeats`` caps identical calls (same tool and arguments).
    """

    def __init__(self, max_tool_calls: int | None, max_repeats: int | None) -> None:
        for label, value in (("max_tool_calls", max_tool_calls), ("max_repeats", max_repeats)):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 1
            ):
                raise ValueError(f"{label} must be a positive integer or None")
        self.max_tool_calls = max_tool_calls
        self.max_repeats = max_repeats
        self._lock = threading.Lock()
        self.calls = 0
        self._seen: dict[tuple[str, str | None], int] = {}

    def admit(self, name: str, arguments: Any) -> None:
        """Count one call or raise `StepLimitExceeded` / `LoopDetected`."""
        if isinstance(arguments, str):
            signature = canonical({"json": arguments})
        elif isinstance(arguments, Mapping):
            signature = canonical(arguments)
        else:
            signature = canonical({"value": arguments}) if arguments is not None else None
        run_id = run_id_var.get() or "-"
        with self._lock:
            if self.max_tool_calls is not None and self.calls >= self.max_tool_calls:
                raise StepLimitExceeded(
                    f"reached max_tool_calls={self.max_tool_calls}; refused tool {name!r}",
                    run_id=run_id,
                    limit=self.max_tool_calls,
                )
            key = (name, signature)
            seen = self._seen.get(key, 0)
            if signature is not None and self.max_repeats is not None and seen >= self.max_repeats:
                raise LoopDetected(
                    f"tool {name!r} was already called {seen} time(s) with the same "
                    f"arguments (max_repeats={self.max_repeats})",
                    run_id=run_id,
                    name=name,
                    repeats=seen,
                )
            self.calls += 1
            self._seen[key] = seen + 1

    def reset(self) -> None:
        """Start counting from zero (a new run)."""
        with self._lock:
            self.calls = 0
            self._seen.clear()
